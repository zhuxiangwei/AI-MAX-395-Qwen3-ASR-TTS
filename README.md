# AI-MAX-395-Qwen3-ASR-TTS

运行在 AI 推理机上的语音交互与监控播报系统。单进程双线程架构：语音助手循环 + 硬件监控播报，共享 TTS 队列，TTS 播放时麦克风自动暂停防回声。

> 模型基础部署见 [AI-MAX-395-Qwen3.6](https://github.com/yourname/AI-MAX-395-Qwen3.6) 项目（llama.cpp + Vulkan，Qwen3.6 27B/35B）。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                 ai_station_angle.py                          │
│                  单进程 · 双线程                              │
├───────────────────────┬─────────────────────────────────────┤
│  主线程: 语音助手       │  监控线程: 硬件+日志监控             │
│                       │                                     │
│ Mic→VAD→ASR→唤醒词    │  router.log / hw-temp.log /proc     │
│   →LLM→TTS→播放       │   →阈值判断→TTS播报(优先级插队)       │
│                       │                                     │
│  TTS 播放时 Mic 暂停  │                                     │
├───────────────────────┴─────────────────────────────────────┤
│  ASR :12347 (Qwen3-ASR-1.7B, CPU)                          │
│  TTS :12348 (Qwen3-TTS-0.6B, vivian)                       │
│  LLM :12345 (router, 278 alias)                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 快速开始

```bash
python3 ai_station_angle.py                 # 完整模式（语音助手 + 监控）
python3 ai_station_angle.py --monitor-only  # 仅监控播报
python3 ai_station_angle.py --voice-only    # 仅语音助手
python3 ai_station_angle.py --test-wake     # 测试唤醒词检测
```

---

## 语音助手核心流程

1. **麦克风监听** — dsnoop 录音 (44100Hz/2ch/S16_LE)，VAD 阈值 RMS≥500
2. **唤醒词检测** — ASR 转文字后正则匹配 "你好小智"/"小智你好"，命中后播预录音回复
3. **指令录音** — 唤醒后进入指令模式，静默 4.0s 截断（唤醒词模式 2.5s）
4. **LLM 对话** — 发送到 router `:12345` (model=278)，支持思考模式
5. **TTS 播放** — 非流式，speaker=vivian，seed=42，播完后等 3s 防回声

---

## 配置常量速查

| 类别 | 参数 | 值 |
|------|------|-----|
| **录音** | 设备/采样率/声道 | `default_capture` / 44100 / 2ch |
| **VAD** | 阈值/连续帧 | RMS≥500 / 5帧 |
| **唤醒词** | 静默截断/模式 | 2.5s / "你好小智"/"小智你好" |
| **指令** | 静默截断/最长时间 | 4.0s / 30s |
| **ASR** | 模型/端口/语言 | Qwen3-ASR-1.7B-Q8_0 / :12347 / chinese |
| **LLM** | 端口/别名/API Key | :12345 / 278 / YOUR_API_KEY |
| **TTS** | 模型/端口/音色/seed | Qwen3-TTS-0.6B / :12348 / vivian / 42 |
| **监控** | 快/慢/日常间隔 | 180s / 300s / 1800s |
| **硬件阈值** | GPU/CPU/MEM | WARN 80°C / WARN 80°C / WARN 80% |
| **硬件阈值** | GPU/CPU/MEM (CRIT) | 90°C / 90°C / 90% |

---

## 监控播报

监控线程三频率轮询硬件温度和日志异常：
- **快速 180s**：GPU/CPU 温度、内存使用率
- **慢速 300s**：NVMe/网卡/WiFi 温度
- **日常 1800s**：综合播报

告警去重 300s，冷却 300s，状态持久化到 `~/.config/monitor-broadcast/state.json`。

---

## systemd 服务

### ai-station.service

```ini
[Unit]
Description=AI Station - Voice Assistant + Monitor Broadcast
After=qwen3-tts.service qwen3-asr.service llama-router.service
Wants=qwen3-tts.service qwen3-asr.service llama-router.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/$USER/scripts/ai_station_angle.py
Restart=on-failure
RestartSec=10
KillMode=process

[Install]
WantedBy=default.target
```

### qwen3-asr.service

```ini
[Unit]
Description=Qwen3-ASR-1.7B STT Service
After=network.target

[Service]
Type=simple
ExecStart=/home/$USER/scripts/qwen3-asr.sh
LimitMEMLOCK=infinity
Restart=on-failure
RestartSec=10
KillMode=process
TimeoutStopSec=15

[Install]
WantedBy=default.target
```

### qwen3-tts.service

```ini
[Unit]
Description=Qwen3-TTS Server (0.6B)
After=network.target

[Service]
Type=simple
ExecStart=/home/$USER/scripts/qwen3-tts.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

---

## 启动脚本要点

**qwen3-asr.sh** — llama-server 纯 CPU 8线程：
- 模型: `Qwen3-ASR-1.7B-Q8_0.gguf` + mmproj
- 参数: `--n-gpu-layers 0 --threads 8 --ctx-size 65536 --parallel 1 --mlock --no-cache-idle-slots --cache-ram 0 --timeout 600`

**qwen3-tts.sh** — 自编译 qwen_tts 引擎：
- 模型目录: `Qwen3-TTS-12Hz-0.6B-CustomVoice`
- 参数: `--serve 12348 -d MODEL_DIR -j 8 -S`（非流式）

---

## 音频配置

声卡 card1 ALC245 Analog，`/etc/asound.conf` 配置 dmix（播放共享）+ dsnoop（录音共享），均为 44100/S16_LE/2ch。系统音量 100%（`alsactl store` 持久化）。

---

## 文件结构

```
.
├── ai_station_angle.py          # 主程序（语音助手+监控，~900行）
├── assets/
│   ├── quick_reply_0~3.wav      # 简短回复预录音
│   ├── sound_asr_done.wav       # ASR→LLM 提示音
│   ├── sound_llm_done.wav       # LLM→TTS 提示音
│   └── wake_reply.wav           # 唤醒词回复预录音
├── qwen3-asr.sh                 # ASR 启动脚本
├── qwen3-tts.sh                 # TTS 启动脚本
└── hw-temp.sh                   # 硬件温度采集
```

---

## 启动依赖链

```
llama-router (:12345) → qwen3-tts (:12348) + qwen3-asr (:12347) → ai-station
```

---

## License

Apache 2.0（模型权重遵循 Qwen 官方 License）
