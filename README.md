# AI-MAX-395-Qwen3-ASR-TTS

运行在 AI 推理机上的语音交互与监控播报系统。单进程双线程架构：语音助手循环 + 硬件监控播报，共享 TTS 队列，TTS 播放时麦克风自动暂停，避免 ASR 听到回声。

> 模型基础部署见 [AI-MAX-395-Qwen3.6](https://github.com/yourname/AI-MAX-395-Qwen3.6) 项目（llama.cpp + Vulkan，Qwen3.6 27B/35B）。

---

## 系统架构

```
麦克风 (card 1 ALC245)
  → VAD (RMS 能量阈值)
    → WAV (44100/2ch/S16_LE)
      → ASR (Qwen3-ASR-1.7B, port 12347)
        → 识别文本
          → 唤醒词检测（"你好小智" / "小智你好"）
            → 播放唤醒回复 → 提取指令
              → LLM (Router port 12345, 别名 358)
                → tool calling（6 个工具）
                  → 回复文本
                    → TTS (Qwen3-TTS 0.6B, port 12348)
                      → WAV → aplay → 扬声器

监控播报（独立线程，共享 TTS 队列）
  → router.log 解析 + hw-temp.log 解析 + /proc 采集
    → 阈值/关键字判断
      → TTS 队列（优先级抢占，最大 3 条）
        → TTS → aplay → 扬声器
```

**核心约束：** ASR 和 TTS 纯 CPU 运行，LLM 在 GPU 上运行。语音处理不竞争 GPU 资源。

---

## 快速开始

```bash
# 完整模式（语音助手 + 监控播报）
python3 ai_station_angle.py

# 仅监控播报
python3 ai_station_angle.py --monitor-only

# 仅语音助手
python3 ai_station_angle.py --voice-only

# 测试模式
python3 ai_station_angle.py --calibrate    # 校准麦克风
python3 ai_station_angle.py --test-mic     # 测试录音
python3 ai_station_angle.py --test-asr     # 测试录音 + ASR
python3 ai_station_angle.py --test-llm     # 测试录音 + ASR + LLM
python3 ai_station_angle.py --test-wake    # 测试唤醒词
```

---

## 语音助手

### 工作流程

1. 持续监听麦克风，检测到语音后录音
2. ASR 识别为文本（严格过滤：无效输出、太短、无意义单字/语气词）
3. 检查是否包含唤醒词（"你好小智" / "小智你好"）
4. 命中唤醒词 → 播放唤醒回复（278 常驻，无需预热）
5. 提取唤醒词后的指令（无指令则再录一段）
6. 播放 ASR→LLM 环节提示音 → LLM 对话（支持 tool calling）
7. 播放 LLM→TTS 环节提示音 → TTS 语音输出
8. 检测到结束词（再见/拜拜/bye/走了/不聊了/先这样）→ TTS 回复 → 回到等待唤醒词状态
9. TTS 播放期间麦克风暂停，避免回声

### 语音活动检测 (VAD)

- 左声道 RMS 能量阈值检测（默认 500，建议 `--calibrate` 校准）
- 帧大小：30ms（1323 samples）
- VAD 连续帧过滤：5 帧连续超阈值才算语音开始（过滤偶发尖峰噪音）
- 静音超时：唤醒词 2.5s / 指令 4.0s 自动停止
- 预语音缓冲：0.3s（防止截断开头）
- 最大录音：30s，最小语音：0.3s
- 监听超时：60s 无语音自动返回
- 校准模式：`--calibrate` 录制 2s 环境噪声，自动建议阈值

### ASR 语音识别

- 模型：Qwen3-ASR-1.7B-Q8_0（纯 CPU，port 12347）
- 接口：OpenAI 兼容 `/v1/audio/transcriptions`
- 输出格式：`<asr_text>` 标签解析
- 严格过滤：无效输出模式匹配、最小长度检查（≥2字）、无意义单字/语气词过滤

### LLM 对话

- 模型：Qwen3.6-27B（GPU，Router port 12345，别名 278）— 常驻不 sleep，无需预热
- 参数：temperature=0.6, top_p=0.95, max_tokens=1024
- 对话历史：最多保留最近 10 轮
- Prewarm：278 常驻模式，无需预热
- Tool Calling：最多 5 轮，防止死循环
- 结束词检测：识别到"再见/拜拜/bye/走了/不聊了/先这样"后 TTS 回复并回到等待唤醒词状态

### 可用工具

| 工具 | 用途 | 数据源 |
|------|------|--------|
| `get_time` | 当前日期时间 | 本地 datetime |
| `get_system_info` | 内存/CPU/GPU/磁盘/服务状态 | /proc, rocm-smi, systemctl |
| `web_search` | 互联网搜索 | Bing 直接爬取 |
| `get_weather` | 城市天气 | 内部调 web_search |
| `browse_url` | 抓取网页内容 | Playwright headless Chromium |
| `calculator` | 数学计算 | math 模块 |

所有工具均为国内可用方案（Bing 搜索，无被墙服务）。

### TTS 语音合成

- 模型：Qwen3-TTS-12Hz-0.6B-CustomVoice（纯 CPU，port 12348）
- 音色：vivian（中文女声）
- 播放策略：非流式 WAV 播放（默认，优先稳定性）
- 流式预缓冲模式保留（`TTS_USE_STREAM = True` 开关）
- 固定种子 seed=42，确保同一文本音色一致

---

## 监控播报

### 监控对象

- **GPU**：温度、负载（`hw-temp.log` + `router.log`）
- **CPU**：温度、负载（`hw-temp.log` + `/proc/loadavg`）
- **内存**：使用率（`/proc/meminfo`）
- **日志告警**：OOM、Xid、segfault、Vulkan 崩溃、Fatal、Error

### 双频率轮询

| 轮询类型 | 间隔 | 监控内容 |
|----------|------|----------|
| 快速轮询 | 180s | 模型切换、推理任务生命周期 |
| 慢速轮询 | 300s | 硬件告警、日志 E/F 级别、日常播报 |

### 告警阈值

| 指标 | 警告 | 严重 |
|------|------|------|
| GPU 温度 | 80°C | 90°C |
| CPU 温度 | 80°C | 90°C |
| 内存使用 | 80% | 90% |

### 播报策略

- **去重**：同类严重告警 5 分钟内不重复
- **优先级抢占**：severity ≥ 3 的严重告警直接插入 TTS 队列头部
- **TTS 队列**：上限 3 条，超出丢弃
- **全局冷却**：非严重告警 5 分钟冷却期
- **日常播报**：30 分钟间隔，包含 CPU/GPU/内存状态

---

## systemd 服务

### 语音助手 + 监控（合一）

**文件：** `~/.config/systemd/user/ai-station.service`

```ini
[Unit]
Description=AI Station - Voice Assistant + Monitor Broadcast
After=network.target qwen3-tts.service qwen3-asr.service llama-router.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/zxw/AI-MAX-395-Qwen3-ASR-TTS/ai_station_angle.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

### 依赖服务

| 服务 | 端口 | 用途 |
|------|------|------|
| `llama-router.service` | 12345 | LLM Router（278 + 358） |
| `qwen3-tts.service` | 12348 | TTS 语音合成 |
| `qwen3-asr.service` | 12347 | ASR 语音识别 |
| `hw-temp.service` | - | 硬件温度/负载日志 |
| `llama-tunnel.service` | - | SSH 反向隧道 |

---

## 文件结构

```
AI-MAX-395-Qwen3-ASR-TTS/
├── ai_station_angle.py    # 主程序（语音助手 + 监控播报合一）
├── assets/
│   ├── wake_reply.wav     # 唤醒回复预缓存音频
│   ├── wake_reply.bak.wav # 唤醒回复备份
│   ├── quick_reply_*.wav  # 随机简短回复（4个）
│   ├── sound_asr_done.wav # ASR→LLM 环节提示音
│   └── sound_llm_done.wav # LLM→TTS 环节提示音
├── llama-router.sh        # Router 启动脚本
├── qwen3-tts.sh           # TTS 启动脚本
├── qwen3-asr.sh           # ASR 启动脚本
├── hw-temp.sh             # 硬件温度监控脚本
└── README.md
```

---

## 音频配置

- **录音设备**：`default_capture`（card 1 ALC245 Analog，3.5mm 麦克风）
- **播放设备**：`default`（card 1 ALC245 Analog 扬声器）
- **ALSA 配置**：`/etc/asound.conf` 配置 dmix（播放共享）和 dsnoop（录音共享）
- **音量**：ALSA Master 100%（alsactl store 持久化），TTS 不管理音量

---

## 日志

| 日志文件 | 内容 |
|----------|------|
| `/home/zxw/logs/monitor/monitor.log` | 监控播报日志（5MB × 3 轮转） |
| `/home/zxw/logs/llama/router.log` | LLM Router 日志 |
| `/home/zxw/logs/llama/asr.log` | ASR 日志 |
| `/home/zxw/logs/hw-temp.log` | 硬件温度/负载日志（每 60s） |

---

## 环境变量与配置

程序内配置常量（位于 `ai_station_angle.py` 头部）：

- `SILENCE_THRESHOLD`：VAD 能量阈值（默认 1000，建议 `--calibrate` 校准）
- `TTS_USE_STREAM`：流式 TTS 开关（默认 False）
- `FAST_POLL` / `SLOW_POLL`：监控轮询间隔
- `GPU_TEMP_WARN` / `GPU_TEMP_CRIT`：告警阈值
- `COMMAND_TIMEOUT`：唤醒后等待指令超时（默认 15s）

---

*AMD Ryzen AI Max+ 395 · 128 GB · Ubuntu 26.04 · Python 3.10+*
