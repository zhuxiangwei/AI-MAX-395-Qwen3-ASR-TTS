#!/usr/bin/env python3
"""
AI 台灯 — 语音助手 + 监控播报 合一程序

单进程双线程架构：
  - 主线程：语音助手循环（Mic → VAD → ASR → 唤醒词 → LLM → TTS → 播放）
  - 监控线程：硬件 + 日志监控，异常时 TTS 播报

共享 TTS 队列，避免 TTS 播放互相干扰；
TTS 播放时麦克风自动暂停，避免 ASR 听到回声。

用法:
  python3 ai_station_angle.py                     # 完整模式（语音助手 + 监控）
  python3 ai_station_angle.py --monitor-only      # 仅监控播报
  python3 ai_station_angle.py --voice-only        # 仅语音助手
  python3 ai_station_angle.py --calibrate         # 校准麦克风
  python3 ai_station_angle.py --test-mic          # 测试录音
  python3 ai_station_angle.py --test-asr          # 测试录音+ASR
  python3 ai_station_angle.py --test-llm          # 测试录音+ASR+LLM
  python3 ai_station_angle.py --test-wake         # 测试唤醒词检测
"""
import argparse
import http.client
import json
import logging
import math
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ============================================================
# 配置
# ============================================================

# --- 路径 ---
LOG_DIR = Path("/home/zxw/logs")
ROUTER_LOG = LOG_DIR / "llama" / "router.log"
HW_TEMP_LOG = LOG_DIR / "hw-temp.log"
STATE_FILE = Path("/home/zxw/.config/monitor-broadcast/state.json")
MONITOR_LOG_DIR = LOG_DIR / "monitor"
MONITOR_LOG_FILE = MONITOR_LOG_DIR / "monitor.log"
WAKE_REPLY_PATH = Path(__file__).parent / "assets" / "wake_reply.wav"

# --- TTS ---
TTS_HOST = "127.0.0.1"
TTS_PORT = 12348
TTS_SPEAKER = "vivian"
TTS_SEED = 42
TTS_USE_STREAM = False
TTS_SAMPLE_RATE = 24000
TTS_BYTES_PER_SEC = TTS_SAMPLE_RATE * 2
TTS_QUEUE_MAX = 3
HTTP_TIMEOUT = 30

# --- 监控轮询 ---
FAST_POLL = 180
SLOW_POLL = 300
COOLDOWN = 300
ALERT_DEDUP = 300
DAILY_INTERVAL = 1800

# --- 硬件阈值 ---
GPU_TEMP_WARN = 80
GPU_TEMP_CRIT = 90
CPU_TEMP_WARN = 80
CPU_TEMP_CRIT = 90
MEM_WARN_PCT = 80
MEM_CRIT_PCT = 90

# --- 录音 ---
RECORD_DEVICE = "default_capture"
RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_MS = 30
FRAME_SAMPLES = int(RATE * FRAME_MS / 1000)
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * SAMPLE_WIDTH
SILENCE_THRESHOLD = 500
VAD_CONSEC_FRAMES = 5  # 需要连续 N 帧超过阈值才算语音开始（过滤偶发尖峰噪音）
SILENCE_DURATION_WAKE = 2.5   # 唤醒词录音：短停顿即截断（唤醒词本身短）
SILENCE_DURATION_CMD = 4.0     # 指令录音：长停顿才截断（支持多句话一次说完）
SILENCE_DURATION = SILENCE_DURATION_WAKE  # 默认用唤醒词阈值
PRE_SPEECH_BUFFER = 0.3
MAX_RECORD_SECONDS = 30
MIN_SPEECH_SECONDS = 0.3
LISTEN_TIMEOUT = 60
WARMUP_FRAMES = 10
OUTPUT_DIR = Path("/tmp/voice_assistant")

def _cleanup_temp_recordings():
    """启动时清理旧的临时录音文件"""
    if OUTPUT_DIR.exists():
        removed = 0
        for f in OUTPUT_DIR.glob("rec_*.wav"):
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
        if removed:
            print(f"[Mic] 清理 {removed} 个旧录音文件")

_cleanup_temp_recordings()

# --- 简短回复预录音（随机选一个播放）---
QUICK_REPLY_FILES = [
    Path(__file__).parent / "assets" / "quick_reply_0.wav",
    Path(__file__).parent / "assets" / "quick_reply_1.wav",
    Path(__file__).parent / "assets" / "quick_reply_2.wav",
    Path(__file__).parent / "assets" / "quick_reply_3.wav",
]


_quick_reply_last_time = 0  # 上次播放 quick_reply 的时间戳
QUICK_REPLY_COOLDOWN = 60  # 冷却秒数，避免反复触发

# --- 预生成提示音（不调 TTS，直接播放 WAV 表示工作状态）---
SOUND_ASR_DONE = Path(__file__).parent / "assets" / "sound_asr_done.wav"      # ASR→LLM 环节提示
SOUND_LLM_DONE = Path(__file__).parent / "assets" / "sound_llm_done.wav"      # LLM→TTS 环节提示

def _play_sound(wav_path):
    """播放短提示音（非 TTS），表示 ASR→LLM 或 LLM→TTS 环节切换。"""
    if not wav_path.exists():
        return
    try:
        subprocess.run(
            ["aplay", "-q", "-D", "default", str(wav_path)],
            check=True, capture_output=True, timeout=5,
        )
    except Exception as e:
        print(f"[Sound] 播放失败: {e}")


def _play_quick_reply():
    """播放随机简短回复 WAV（带冷却，找不到不播）。"""
    global _quick_reply_last_time
    now = time.time()
    if now - _quick_reply_last_time < QUICK_REPLY_COOLDOWN:
        return  # 冷却中，静默跳过
    available = [f for f in QUICK_REPLY_FILES if f.exists()]
    if not available:
        print("[VA] quick_reply WAV 文件全部不存在，跳过")
        return
    chosen = random.choice(available)
    print(f"[VA] 播放 quick_reply: {chosen.name}")
    _quick_reply_last_time = now
    _play_wav_sync(chosen)
    time.sleep(6)


def _tts_speak_now(text, extra_delay=0):
    """同步 TTS：直接调 TTS 服务，等播完才返回。
    语音助手所有播报统一用这个，不再走异步队列。
    extra_delay：播完后额外等待秒数，防止扬声器回声。
    """
    est_duration = estimate_audio_duration(text)
    payload = _build_tts_payload(text)
    wav_path = None
    _tts_playing.set()
    try:
        tts_timeout = max(int(est_duration * 5) + 30, HTTP_TIMEOUT)
        conn = http.client.HTTPConnection(TTS_HOST, TTS_PORT, timeout=tts_timeout)
        conn.request("POST", "/v1/tts", body=payload,
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            print(f"[TTS] HTTP {resp.status}: {resp.read()[:200]}")
            return
        wav_data = resp.read()
        conn.close()
        if not wav_data or len(wav_data) < 44:
            print(f"[TTS] 返回数据过短: {len(wav_data)} bytes")
            return
        wav_path = f"/tmp/tts_va_{int(time.time()*1000)}.wav"
        with open(wav_path, "wb") as f:
            f.write(wav_data)
        print(f'[TTS] 同步播报: {text[:30]}... (估时 {est_duration:.1f}s)')
        subprocess.run(
            ["aplay", "-q", "-D", "default", wav_path],
            check=True, capture_output=True,
            timeout=int(est_duration * 2 + 30),
        )
    except Exception as e:
        print(f"[TTS] 同步播报失败: {e}")
    finally:
        _tts_playing.clear()
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)

# --- 唤醒词 ---
WAKE_PATTERNS = [
    r"你好[，,]?\s*小智",
    r"小智[，,]?\s*你好",
    r"你好小智",
    r"小智你好",
]
WAKE_REGEX = re.compile("|".join(WAKE_PATTERNS))
WAKE_REPLY = "我是宇宙超级智多星，小智在此等候多时了！想问什么你说"
COMMAND_TIMEOUT = 15

# --- LLM ---
ROUTER_HOST = "127.0.0.1"
ROUTER_PORT = 12345
ROUTER_API_KEY = "71769f2CeCE681015e1B71eCf848900e"
LLM_MODEL = "278"
MAX_TOOL_ROUNDS = 5

# --- ASR ---
ASR_URL = "http://127.0.0.1:12347/v1/audio/transcriptions"
ASR_MODEL = "Qwen3-ASR-1.7B"
ASR_TIMEOUT = 60
ASR_LANGUAGE = "chinese"
ASR_PROMPT = "以下是普通话的语音转文字结果。"

# ASR 无效输出过滤（llama.cpp 默认 prompt 回声等）
_ASR_INVALID_PATTERNS = [
    "transcribe audio to text",
    "transcribe audio a texto",
    "transcribe audio",
    "以下是普通话",
    "speech to text",
    "语音转文字",
]

# ASR 结果最小有效长度（单字或无意义短词直接丢弃）
ASR_MIN_VALID_LEN = 2
# ASR 无意义输出（单字/语气词）
_ASR_MEANINGLESS = {
    "嗯", "啊", "哦", "呃", "唉", "嗨", "哈", "嘿", "唔", "呀",
    "呢", "吧", "吗", "嗯。", "啊。", "哦。", "嗯,", "啊,",
    "hello", "hi", "ok", "yeah", "嗯嗯", "啊哈",
}

# --- 正则 ---
ALERT_KEYWORDS = [
    (re.compile(r'(?i)out.of.memory|OOM|cannot.allocate'), "oom", 3),
    (re.compile(r'(?i)xid'), "xid_error", 3),
    (re.compile(r'(?i)segfault|segmentation.fault'), "segfault", 3),
    (re.compile(r'(?i)DeviceLost|vk::.*Error|vulkan.*fail'), "vulkan_crash", 3),
    (re.compile(r'(?i)child.*crash|child.*exit|defunct|killed.*child'), "child_crash", 3),
    (re.compile(r'\] \d+\.\d+\.\d+\.\d+ F '), "fatal", 3),
    (re.compile(r'\] \d+\.\d+\.\d+\.\d+ E '), "error", 2),
]
TASK_LAUNCH_RE = re.compile(r'launch_slot_.*?task\s+(\d+)')
TASK_RELEASE_RE = re.compile(r'release:.*?task\s+(\d+).*?n_tokens\s*=\s*(\d+)')
TASK_GEN_RE = re.compile(r'print_timing.*?task\s+(\d+).*?n_decoded\s*=\s*(\d+).*?tg\s*=.*?([\\d.]+)')
TASK_IDLE_RE = re.compile(r'all slots are idle')
MODEL_PROXY_RE = re.compile(r'proxying request to model\s+(.+?)\s+on port\s+(\d+)')
HW_TEMP_LOG_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+'
    r'gpu_busy=(\d+)%\s+'
    r'gpu=(\d+)°C\s+'
    r'cpu=(\d+)°C\s+'
    r'nvme=(\d+)°C\s+'
    r'r8169=(\d+)°C\s+'
    r'eno1=(\d+)°C\s+'
    r'wifi=(\d+)°C'
)

# --- 全局状态 ---
_stop_event = threading.Event()
_tts_playing = threading.Event()  # TTS 正在播放时置位
_voice_busy = threading.Event()  # 语音助手链路进行中（唤醒→回复→录指令→LLM→TTS+3s）


def _handle_sigterm(signum, frame):
    _stop_event.set()
    os.write(sys.stderr.fileno(), f"[AI Station] 收到信号 {signum}，准备退出...\n".encode())


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# ============================================================
# 日志
# ============================================================

def setup_logging():
    MONITOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler = RotatingFileHandler(
        MONITOR_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stderr_handler)


# ============================================================
# 播报文案
# ============================================================

class BroadcastTexts:
    OOM = ["严重警告！显存溢出！", "紧急！OOM 告警！"]
    XID_ERROR = ["显卡报错！Xid 错误！", "检测到 Xid 错误！"]
    CRITICAL = ["紧急告警！严重错误！", "Critical 错误！"]
    SEGFAULT = ["段错误！进程可能崩溃！", "Segmentation Fault！"]
    FATAL = ["致命错误！", "Fatal 告警！"]
    VULKAN_CRASH = ["Vulkan 崩溃！", "GPU 驱动异常！"]
    CHILD_CRASH = ["子进程崩溃！", "推理进程异常退出！"]
    ERROR = ["检测到错误日志。", "出现 Error。"]
    HW_GPU_TEMP_CRIT = ["温度严重告警！{temp} 度！", "设备过热！{temp} 度！"]
    HW_GPU_TEMP_WARN = ["温度 {temp} 度，偏高。", "温度 {temp} 度。"]
    HW_CPU_TEMP_CRIT = ["CPU 温度严重告警！{temp} 度！", "CPU 过热！{temp} 度！"]
    HW_CPU_TEMP_WARN = ["CPU 温度 {temp} 度，偏高。", "CPU 温度 {temp} 度。"]
    HW_MEM_CRIT = ["内存严重不足！{pct}%！", "内存快满了，{pct}%！"]
    HW_MEM_WARN = ["内存使用偏高，{pct}%。", "内存占用 {pct}%。"]
    MODEL_SWITCH = ["模型已切换到 {model}。", "当前模型：{model}。"]
    TASK_START = ["新任务 {task_id}，开始推理。", "任务 {task_id} 启动。"]
    TASK_PREFILL = ["任务 {task_id}，预填充完成，{tokens} 个 token，速度 {speed} t/s。"]
    TASK_DONE = ["任务 {task_id} 完成，共 {tokens} 个 token。", "任务 {task_id} 结束。"]
    TASK_IDLE = ["所有任务完成，系统空闲。", "空闲了。"]
    TASK_GEN_MILESTONE = ["任务 {task_id} 生成中，已输出 {decoded} token，速度 {speed} t/s。"]
    QUIET = ["一切正常。", "系统运行平稳。", "风平浪静。"]
    BUSY = ["忙着呢。", "有任务在跑。"]

    @classmethod
    def pick(cls, category, **kwargs):
        texts = getattr(cls, category, [])
        if not texts:
            return None
        text = random.choice(texts)
        return text.format(**kwargs) if kwargs else text


# ============================================================
# 工具函数
# ============================================================

def num_to_chinese(n):
    cn_digits = '零一二三四五六七八九'
    return ''.join(cn_digits[int(d)] for d in str(n))


def estimate_audio_duration(text):
    duration = 0.0
    for m in re.finditer(r'\d+\.?\d*', text):
        duration += len(m.group()) * 0.15
    no_num = re.sub(r'\d+\.?\d*', '', text)
    cn_count = sum(1 for ch in no_num if '\u4e00' <= ch <= '\u9fff')
    en_count = sum(1 for ch in no_num if ch.isascii() and ch.isalpha())
    other = len(no_num) - cn_count - en_count
    duration += cn_count * 0.30 + en_count * 0.04 + other * 0.08
    return max(duration, 0.5)


def _build_tts_payload(text):
    return json.dumps({
        "text": text,
        "speaker": TTS_SPEAKER,
        "language": "chinese",
        "seed": TTS_SEED,
    }, ensure_ascii=False).encode("utf-8")


# ============================================================
# 系统状态采集
# ============================================================

def get_cpu_load():
    try:
        with open("/proc/loadavg", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def get_system_memory():
    try:
        with open("/proc/meminfo", "r") as f:
            info = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 1)
        available = info.get("MemAvailable", 0)
        return round((1 - available / total) * 100, 1)
    except Exception:
        return None


def get_hw_status():
    try:
        with open(HW_TEMP_LOG, "r") as f:
            lines = f.readlines()
        for line in reversed(lines):
            m = HW_TEMP_LOG_RE.search(line)
            if m:
                return {
                    "gpu_busy": int(m.group(2)),
                    "gpu_temp": int(m.group(3)),
                    "cpu_temp": int(m.group(4)),
                    "nvme_temp": int(m.group(5)),
                    "r8169_temp": int(m.group(6)),
                    "eno1_temp": int(m.group(7)),
                    "wifi_temp": int(m.group(8)),
                }
    except Exception:
        pass
    return None


def build_daily_report():
    parts = []
    cpu_load = get_cpu_load()
    if cpu_load is not None:
        parts.append(f"CPU 负载 {cpu_load:.2f}")
    hw = get_hw_status()
    if hw:
        parts.append(f"GPU {hw['gpu_busy']}%")
        parts.append(f"GPU 温度 {hw['gpu_temp']} 度")
        parts.append(f"CPU 温度 {hw['cpu_temp']} 度")
    mem_pct = get_system_memory()
    if mem_pct is not None:
        parts.append(f"内存 {mem_pct}%")
    return "，".join(parts) + "。" if parts else None


# ============================================================
# TTS 队列（共享）
# ============================================================

class TTSQueue:
    def __init__(self):
        self._queue = deque()
        self._lock = threading.Lock()
        self._worker = None
        self._sent_count = 0
        self._fail_count = 0

    def start(self):
        self._worker = threading.Thread(target=self._run, daemon=True, name="tts-worker")
        self._worker.start()

    def put(self, text, priority=0, tag=""):
        # 语音助手链路进行中时，丢弃所有监控播报（不打断对话）
        if _voice_busy.is_set():
            logging.info(f'[Queue] 丢弃: "{text[:40]}" (语音助手占用)')
            return
        with self._lock:
            if len(self._queue) >= TTS_QUEUE_MAX:
                logging.info(f'[Queue] 丢弃: "{text[:40]}" (队列已满)')
                return
            if priority >= 3:
                idx = 0
                for i, item in enumerate(self._queue):
                    if item[1] < 3:
                        idx = i
                        break
                else:
                    idx = len(self._queue)
                self._queue.insert(idx, (text, priority, tag))
            else:
                self._queue.append((text, priority, tag))
        logging.info(f'[Queue] +"{text[:40]}" (pri={priority})')

    def _run(self):
        while not _stop_event.is_set():
            item = None
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
            if item is None:
                time.sleep(1)
                continue
            text, priority, tag = item
            self._speak(text)
            self._sent_count += 1

    def _speak(self, text):
        if TTS_USE_STREAM:
            self._speak_prebuffer(text)
        else:
            self._speak_wav(text)

    def _speak_wav(self, text):
        est_duration = estimate_audio_duration(text)
        logging.info(f"[TTS] 非流式: \"{text[:50]}\" (估时 {est_duration:.1f}s)")
        payload = _build_tts_payload(text)
        t0 = time.time()
        wav_path = None
        conn = None
        try:
            tts_timeout = max(int(est_duration * 5) + 30, HTTP_TIMEOUT)
            conn = http.client.HTTPConnection(TTS_HOST, TTS_PORT, timeout=tts_timeout)
            conn.request("POST", "/v1/tts", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                logging.error(f"[TTS] HTTP {resp.status}: {resp.read()[:200]}")
                return
            wav_data = resp.read()
            if not wav_data or len(wav_data) < 44:
                logging.error(f"[TTS] 返回数据过短: {len(wav_data)} bytes")
                return
            wav_path = f"/tmp/tts_{os.getpid()}_{int(time.time())}.wav"
            with open(wav_path, "wb") as f:
                f.write(wav_data)
            logging.info(f"[TTS] 生成完成: {len(wav_data)} bytes, {time.time()-t0:.1f}s")
            # 语音助手链路进行中，TTS 生成完也不播放（丢弃）
            if _voice_busy.is_set():
                logging.info(f"[TTS] 丢弃已生成 TTS（语音助手占用）")
                return
            # 播放 — 置位 playing 标志，语音助手会等待
            _tts_playing.set()
            aplay = subprocess.Popen(
                ["aplay", "-q", "-D", "default", wav_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            _, stderr = aplay.communicate(timeout=est_duration * 3 + 30)
            if aplay.returncode != 0 and stderr:
                logging.warning(f"[Play] aplay 返回码 {aplay.returncode}")
            _tts_playing.clear()
            logging.info(f"[TTS] 播放完成: \"{text[:50]}\" ({time.time()-t0:.1f}s)")
        except subprocess.TimeoutExpired:
            _tts_playing.clear()
            logging.warning("[TTS] 播放超时")
        except Exception as e:
            _tts_playing.clear()
            logging.error(f"[TTS] 异常: {e}", exc_info=True)
            self._fail_count += 1
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

    def _speak_prebuffer(self, text):
        """流式预缓冲 TTS（保留模式，默认不使用）。"""
        est_duration = estimate_audio_duration(text)
        prebuffer_sec = max(est_duration * 0.8, 2.0)
        prebuffer_bytes = int(prebuffer_sec * TTS_BYTES_PER_SEC)
        prebuffer_bytes = max(32 * 1024, min(1024 * 1024, prebuffer_bytes))
        logging.info(f"[TTS] 流式: \"{text[:50]}\" (预缓冲 {prebuffer_sec:.1f}s)")
        payload = _build_tts_payload(text)
        t0 = time.time()
        fifo_path = None
        conn = None
        aplay = None
        try:
            conn = http.client.HTTPConnection(TTS_HOST, TTS_PORT, timeout=HTTP_TIMEOUT)
            conn.request("POST", "/v1/tts/stream", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                logging.error(f"[TTS] HTTP {resp.status}: {resp.read()[:200]}")
                conn.close()
                conn = None
                return
            prebuffer = bytearray()
            while len(prebuffer) < prebuffer_bytes:
                chunk = resp.read(32768)
                if not chunk:
                    break
                prebuffer.extend(chunk)
            logging.info(f"[Prebuf] {len(prebuffer)} bytes, {time.time()-t0:.1f}s")
            fifo_path = f"/tmp/tts_fifo_{os.getpid()}_{time.time()}"
            os.mkfifo(fifo_path, 0o600)
            writer_stop = threading.Event()
            def _fifo_writer():
                fd = -1
                try:
                    fd = os.open(fifo_path, os.O_WRONLY)
                    if prebuffer:
                        os.write(fd, bytes(prebuffer))
                    while not writer_stop.is_set():
                        chunk = resp.read(32768)
                        if not chunk:
                            break
                        os.write(fd, chunk)
                finally:
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            writer_thread = threading.Thread(target=_fifo_writer, daemon=True)
            writer_thread.start()
            _tts_playing.set()
            aplay = subprocess.Popen(
                ["aplay", "-q", "-D", "default", "-r", str(TTS_SAMPLE_RATE),
                 "-f", "S16_LE", "-c", "1", fifo_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            writer_thread.join(timeout=est_duration * 3 + 60)
            if not writer_stop.is_set():
                writer_stop.set()
            aplay.wait()
            _tts_playing.clear()
            logging.info(f"[TTS] 流式完成 ({time.time()-t0:.1f}s)")
        except Exception as e:
            _tts_playing.clear()
            logging.error(f"[TTS] 流式异常: {e}", exc_info=True)
            self._fail_count += 1
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if aplay:
                try:
                    aplay.kill()
                    aplay.wait()
                except Exception:
                    pass
            if fifo_path:
                try:
                    os.unlink(fifo_path)
                except OSError:
                    pass

    def wait_playing(self, timeout=30):
        """等待当前 TTS 播放完成。语音助手录音前调用。"""
        _tts_playing.wait(timeout=timeout)


# ============================================================
# 麦克风录音
# ============================================================

def compute_rms(data: bytes) -> float:
    count = len(data) // (SAMPLE_WIDTH * CHANNELS)
    if count == 0:
        return 0.0
    total = 0
    for i in range(count):
        offset = i * SAMPLE_WIDTH * CHANNELS
        sample = struct_unpack_h(data, offset)
        total += sample * sample
    return math.sqrt(total / count)


# struct.unpack 加速
import struct
_struct_h = struct.Struct('<h')
def struct_unpack_h(data, offset):
    return _struct_h.unpack_from(data, offset)[0]


def save_wav(path: Path, pcm_data: bytes):
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(RATE)
        wf.writeframes(pcm_data)


class MicRecorder:
    def __init__(self, device=RECORD_DEVICE, silence_threshold=SILENCE_THRESHOLD,
                 silence_duration=SILENCE_DURATION, max_record=MAX_RECORD_SECONDS,
                 min_speech=MIN_SPEECH_SECONDS, listen_timeout=LISTEN_TIMEOUT):
        self.device = device
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.max_record = max_record
        self.min_speech = min_speech
        self.listen_timeout = listen_timeout
        self._proc = None

    def _start_arecord(self):
        proc = subprocess.Popen(
            ["arecord", "-D", self.device, "-r", str(RATE),
             "-f", "S16_LE", "-c", str(CHANNELS), "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._proc = proc
        return proc

    def _stop_arecord(self):
        if self._proc:
            self._proc.kill()
            self._proc.wait()
            self._proc = None

    def _read_frame(self, proc):
        chunk = proc.stdout.read(FRAME_BYTES)
        if not chunk or len(chunk) < FRAME_BYTES:
            return None
        return chunk

    def _warmup(self, proc):
        for _ in range(WARMUP_FRAMES):
            self._read_frame(proc)

    def calibrate(self, duration=2.0):
        print(f"[VAD] 校准中，请保持安静 {duration}s...")
        proc = self._start_arecord()
        rms_values = []
        target = int(duration * 1000 / FRAME_MS)
        try:
            self._warmup(proc)
            for _ in range(target):
                chunk = self._read_frame(proc)
                if chunk is None:
                    break
                rms_values.append(compute_rms(chunk))
        finally:
            self._stop_arecord()
        if not rms_values:
            return SILENCE_THRESHOLD
        avg = sum(rms_values) / len(rms_values)
        mx = max(rms_values)
        suggested = max(int(avg * 3), 200)
        print(f"[VAD] 噪声: avg={avg:.0f} max={mx:.0f} → 建议阈值: {suggested}")
        return suggested

    def record_once(self, proc=None, silence_duration=None):
        """录音一段。
        
        silence_duration: 可选，覆盖默认静音时长。
        唤醒词用 2.5s，指令用 4.0s。
        """
        own_proc = proc is None
        if own_proc:
            proc = self._start_arecord()
            self._warmup(proc)
        sd = silence_duration if silence_duration is not None else self.silence_duration
        silence_limit = int(sd * 1000 / FRAME_MS)
        print(f"[Mic] 监听中 (threshold={self.silence_threshold}, silence={sd}s)...")
        pre_buffer = []
        pre_buffer_size = int(PRE_SPEECH_BUFFER * 1000 / FRAME_MS)
        frames = []
        is_speaking = False
        silence_frames = 0
        max_frames = int(self.max_record * 1000 / FRAME_MS)
        start_time = time.time()
        try:
            while True:
                # 监控 TTS 正在播放时不录音（避免录到播报声）
                if _tts_playing.is_set():
                    if is_speaking:
                        print("[Mic] TTS 播放中断录音")
                        return None
                    # 还没开始说话，直接跳过这一帧
                    chunk = self._read_frame(proc)
                    if chunk is None:
                        break
                    rms = compute_rms(chunk)
                    pre_buffer.append(chunk)
                    if len(pre_buffer) > pre_buffer_size:
                        pre_buffer.pop(0)
                    continue
                if not is_speaking and self.listen_timeout > 0:
                    if time.time() - start_time > self.listen_timeout:
                        print("[Mic] 监听超时")
                        return None
                chunk = self._read_frame(proc)
                if chunk is None:
                    break
                rms = compute_rms(chunk)
                if not is_speaking:
                    pre_buffer.append(chunk)
                    if len(pre_buffer) > pre_buffer_size:
                        pre_buffer.pop(0)
                    if rms > self.silence_threshold:
                        # 需要连续 VAD_CONSEC_FRAMES 帧超过阈值才算语音开始（过滤偶发尖峰）
                        consec = 1  # 当前帧
                        for c in pre_buffer[-(VAD_CONSEC_FRAMES-1):]:
                            if compute_rms(c) > self.silence_threshold:
                                consec += 1
                            else:
                                break
                        if consec >= VAD_CONSEC_FRAMES:
                            is_speaking = True
                            frames.extend(pre_buffer)
                            frames.append(chunk)
                            print(f"[VAD] 语音开始 (rms={rms:.0f})")
                            silence_frames = 0
                    else:
                        # 不连续，重置
                        pass
                else:
                    frames.append(chunk)
                    if rms < self.silence_threshold:
                        silence_frames += 1
                        if silence_frames >= silence_limit:
                            print(f"[VAD] 语音结束 ({len(frames)} frames)")
                            break
                    else:
                        silence_frames = 0
                    if len(frames) >= max_frames:
                        print(f"[VAD] 达到最大时长 {self.max_record}s")
                        break
            if not is_speaking:
                return None
            raw_pcm = b''.join(frames)
            total_samples = len(raw_pcm) // (SAMPLE_WIDTH * CHANNELS)
            duration = total_samples / RATE
            if duration < self.min_speech:
                print(f"[Mic] 太短 ({duration:.1f}s)，丢弃")
                return None
            wav_path = OUTPUT_DIR / f"rec_{int(time.time())}.wav"
            save_wav(wav_path, raw_pcm)
            print(f"[Mic] 保存: {wav_path} ({duration:.1f}s)")
            return wav_path
        finally:
            if own_proc:
                self._stop_arecord()


# ============================================================
# ASR
# ============================================================

def transcribe(wav_path):
    import requests
    wav_path = Path(wav_path)
    if not wav_path.exists():
        return None
    try:
        with open(wav_path, "rb") as f:
            resp = requests.post(
                ASR_URL,
                files={"file": ("audio.wav", f, "audio/wav")},
                data={
                    "model": ASR_MODEL,
                    "language": ASR_LANGUAGE,
                    "prompt": ASR_PROMPT,
                },
                timeout=ASR_TIMEOUT,
            )
    except requests.exceptions.RequestException as e:
        print(f"[ASR] 请求失败: {e}")
        return None
    finally:
        # 录音文件已读取完毕，删除临时文件
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
    if resp.status_code != 200:
        print(f"[ASR] HTTP {resp.status_code}: {resp.text}")
        return None
    data = resp.json()
    raw_text = data.get("text", "")
    match = re.search(r"<asr_text>(.*)", raw_text)
    if match:
        text = match.group(1).strip()
    else:
        text = re.sub(r"^language\s+\w+\s*", "", raw_text).strip()
    text_lower = text.lower().strip()
    for bad in _ASR_INVALID_PATTERNS:
        if bad in text_lower:
            print(f"[ASR] 丢弃无效输出: {text}")
            return None
    # 严格过滤无效 ASR 输出
    text_stripped = text.strip().rstrip("。.,，！!？?")
    if len(text_stripped) < ASR_MIN_VALID_LEN:
        print(f"[ASR] 太短，丢弃: '{text}'")
        return None
    if text_stripped in _ASR_MEANINGLESS or text.lower().strip() in _ASR_MEANINGLESS:
        print(f"[ASR] 无意义输出，丢弃: '{text}'")
        return None
    print(f"[ASR] 识别: {text}")
    return text if text else None


# ============================================================
# LLM
# ============================================================

SYSTEM_PROMPT = """你是一个简洁的语音助手。回答要求：
1. 口语化，简短直接，通常不超过两三句话
2. 不要使用 markdown、表格、代码块等格式
3. 不要说"作为AI"之类的套话
4. 像和朋友聊天一样自然
5. 如果工具返回了数据，基于数据给出自然简洁的回答，不要复述原始数据格式
6. 必须用中文回答，无论提问语言是什么"""

# 工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前日期、时间和星期。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "获取系统状态：内存、CPU负载、GPU温度、磁盘、服务状态等。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息。",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气信息。",
            "parameters": {"type": "object", "properties": {
                "city": {"type": "string", "description": "城市名称"},
            }, "required": ["city"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_url",
            "description": "抓取指定网页的文本内容。",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "网页URL"},
            }, "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "数学计算器。",
            "parameters": {"type": "object", "properties": {
                "expression": {"type": "string", "description": "数学表达式"},
            }, "required": ["expression"]},
        },
    },
]

# 工具执行
def _get_time():
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return f"当前时间：{now.year}年{now.month}月{now.day}日 {now.hour}时{now.minute}分{now.second}秒 星期{weekdays[now.weekday()]}"


def _get_system_info():
    lines = []
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        total = int(re.search(r"MemTotal:\s+(\d+)", meminfo).group(1))
        avail = int(re.search(r"MemAvailable:\s+(\d+)", meminfo).group(1))
        used = total - avail
        lines.append(f"内存：总计 {total/1024/1024:.0f}GB，已用 {used/1024/1024:.1f}GB，可用 {avail/1024/1024:.1f}GB")
    except Exception as e:
        lines.append(f"内存信息获取失败: {e}")
    try:
        with open("/proc/loadavg") as f:
            lines.append(f"CPU 负载：{f.read().strip()}")
    except Exception:
        pass
    try:
        result = subprocess.run(["rocm-smi", "--showtemp", "--json"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            gpu_data = json.loads(result.stdout)
            for i, card in enumerate(gpu_data.get("card", [])):
                lines.append(f"GPU {i}：温度 {card.get('temp','N/A')}°C，功耗 {card.get('power_avg','N/A')}W")
        else:
            lines.append("GPU 信息：rocm-smi 不可用")
    except FileNotFoundError:
        lines.append("GPU 信息：rocm-smi 未安装")
    except Exception:
        pass
    try:
        result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            parts = result.stdout.strip().split("\n")[1].split()
            lines.append(f"磁盘：总计 {parts[1]}，已用 {parts[2]}，使用率 {parts[4]}")
    except Exception:
        pass
    return "\n".join(lines)


def _web_search(query):
    import requests
    try:
        resp = requests.get("https://www.bing.com/search", params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"},
            timeout=10)
        resp.encoding = "utf-8"
        titles = re.findall(r'<h2[^>]*>(.*?)</h2>', resp.text, re.S)
        snippets = re.findall(r'<p[^>]*>(.*?)</p>', resp.text, re.S)
        clean = lambda t: re.sub(r'<[^>]+>', '', t).strip().replace('\n', ' ')
        results = []
        for i in range(min(len(titles), 5)):
            title = clean(titles[i])
            if len(title) < 5:
                continue
            body = clean(snippets[i]) if i < len(snippets) else ""
            results.append(f"[{i+1}] {title}\n    {body[:200]}")
        if results:
            return f"搜索 '{query}' 的结果：\n" + "\n".join(results)
    except Exception:
        pass
    return f"搜索 '{query}' 未找到结果"


def _get_weather(city):
    return _web_search(f"{city} 天气")


def _browse_url(url):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium-browser",
            )
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            text = page.inner_text("body").strip()
            browser.close()
        return (text[:3000] + "...") if len(text) > 3000 else (text or f"无法获取网页内容（{url}）")
    except ImportError:
        return "错误：playwright 未安装"
    except Exception as e:
        return f"网页抓取失败: {e}"


def _calculator(expression):
    try:
        allowed = {
            "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
            "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "log": math.log, "log10": math.log10, "exp": math.exp,
            "floor": math.floor, "ceil": math.ceil,
            "pi": math.pi, "e": math.e, "factorial": math.factorial,
        }
        result = eval(expression.strip(), {"__builtins__": {}}, allowed)
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"


_TOOL_FN = {
    "get_time": _get_time, "get_system_info": _get_system_info,
    "web_search": _web_search, "get_weather": _get_weather,
    "browse_url": _browse_url, "calculator": _calculator,
}


def execute_tool_calls(tool_calls):
    results = []
    for tc in tool_calls:
        func_name = tc["function"]["name"]
        func_args_str = tc["function"]["arguments"]
        tool_id = tc["id"]
        fn = _TOOL_FN.get(func_name)
        if not fn:
            results.append({"role": "tool", "tool_call_id": tool_id,
                            "content": f"错误：未知工具 '{func_name}'"})
            continue
        try:
            args = json.loads(func_args_str) if func_args_str else {}
            result = fn(**args)
            print(f"[TOOL] {func_name} → {str(result)[:200]}")
            results.append({"role": "tool", "tool_call_id": tool_id, "content": str(result)})
        except Exception as e:
            results.append({"role": "tool", "tool_call_id": tool_id,
                            "content": f"工具 '{func_name}' 执行失败: {e}"})
    return results


# LLM 客户端
from openai import OpenAI
_client = OpenAI(
    base_url=f"http://{ROUTER_HOST}:{ROUTER_PORT}/v1",
    api_key=ROUTER_API_KEY,
    timeout=300,
)
_prewarm_done = False
_prewarm_thread = None


def prewarm():
    """278 常驻不 sleep，无需预热。保留空函数兼容调用。"""
    global _prewarm_done
    _prewarm_done = True
    print(f"[LLM] {LLM_MODEL} 常驻模式，无需预热")


def chat(user_text, history=None):
    if _prewarm_thread and not _prewarm_done:
        print(f"[LLM] 等待 {LLM_MODEL} 唤醒完成...")
        _prewarm_thread.join(timeout=120)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    tool_round = 0
    while tool_round < MAX_TOOL_ROUNDS:
        try:
            response = _client.chat.completions.create(
                model=LLM_MODEL, messages=messages,
                temperature=0.3, top_p=0.95, max_tokens=1024,
                tools=TOOLS,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except Exception as e:
            print(f"[LLM] 请求失败: {e}")
            return None
        choice = response.choices[0]
        msg = choice.message
        if choice.finish_reason == "tool_calls" and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_round += 1
            print(f"[LLM] 第 {tool_round} 轮 tool calls")
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })
            tool_calls_list = [
                {"id": tc.id, "function": {"name": tc.function.name,
                                           "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
            messages.extend(execute_tool_calls(tool_calls_list))
            continue
        text = (msg.content or "").strip()
        if text:
            print(f"[LLM] 回复: {text}")
            return text
        return None
    return "抱歉，处理请求时遇到问题，请稍后再试。"


# ============================================================
# 唤醒词
# ============================================================

def check_wake_word(text):
    return bool(WAKE_REGEX.search(text))


def extract_command(text):
    cmd = WAKE_REGEX.sub("", text).strip()
    cmd = re.sub(r'^[，,。\s]+', '', cmd)
    return cmd


# ============================================================
# 监控播报线程
# ============================================================

class MonitorState:
    def __init__(self):
        self.router_offset = 0
        self.router_error_offset = 0
        self.hw_temp_offset = 0
        self.last_alert_time = 0
        self.last_daily_broadcast = 0
        self.alert_dedup = {}
        self.current_model = None
        self.active_tasks = {}
        self.last_gen_milestone = {}
        self._load()

    def _load(self):
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    data = json.load(f)
                self.router_offset = data.get("router_offset", 0)
                self.router_error_offset = data.get("router_error_offset", 0)
                self.hw_temp_offset = data.get("hw_temp_offset", 0)
                self.last_alert_time = data.get("last_alert_time", 0)
                self.last_daily_broadcast = data.get("last_daily_broadcast", 0)
                self.alert_dedup = data.get("alert_dedup", {})
                self.current_model = data.get("current_model")
        except Exception:
            pass

    def save(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump({
                    "router_offset": self.router_offset,
                    "router_error_offset": self.router_error_offset,
                    "hw_temp_offset": self.hw_temp_offset,
                    "last_alert_time": self.last_alert_time,
                    "last_daily_broadcast": self.last_daily_broadcast,
                    "alert_dedup": self.alert_dedup,
                    "current_model": self.current_model,
                }, f, ensure_ascii=False)
            tmp.rename(STATE_FILE)
        except Exception as e:
            logging.error(f"[Monitor] 状态保存失败: {e}")


def run_monitor(tts_queue):
    """监控播报线程。"""
    state = MonitorState()
    last_fast = time.time()
    last_slow = time.time()
    logging.info("[Monitor] 监控线程启动")

    while not _stop_event.is_set():
        now = time.time()

        # 快速轮询：任务状态
        if now - last_fast >= FAST_POLL:
            last_fast = now
            try:
                _check_router_log(state, tts_queue)
            except Exception as e:
                logging.error(f"[Monitor] 快速轮询异常: {e}")

        # 慢速轮询：硬件 + 日志告警 + 日常
        if now - last_slow >= SLOW_POLL:
            last_slow = now
            try:
                _check_hw_alerts(state, tts_queue)
                _check_router_errors(state, tts_queue)
                _check_daily_broadcast(state, tts_queue)
                state.save()
            except Exception as e:
                logging.error(f"[Monitor] 慢速轮询异常: {e}")

        _stop_event.wait(10)

    logging.info("[Monitor] 监控线程退出")
    state.save()


def _check_router_log(state, tts_queue):
    try:
        size = ROUTER_LOG.stat().st_size
    except Exception:
        return
    if size < state.router_offset:
        state.router_offset = 0
    if size == state.router_offset:
        return
    try:
        with open(ROUTER_LOG, "r") as f:
            f.seek(state.router_offset)
            new_lines = f.readlines()
            state.router_offset = f.tell()
    except Exception:
        return

    for line in new_lines:
        # 模型切换
        m = MODEL_PROXY_RE.search(line)
        if m:
            model_name = m.group(1)
            if model_name != state.current_model:
                state.current_model = model_name
                text = BroadcastTexts.pick("MODEL_SWITCH", model=model_name)
                if text:
                    tts_queue.put(text, priority=1, tag="model_switch")
                continue

        # 任务启动
        m = TASK_LAUNCH_RE.search(line)
        if m:
            task_id = m.group(1)
            state.active_tasks[task_id] = "started"
            tid_cn = num_to_chinese(int(task_id))
            text = BroadcastTexts.pick("TASK_START", task_id=tid_cn)
            if text:
                tts_queue.put(text, priority=1, tag="task_start")
            continue

        # 任务完成
        m = TASK_RELEASE_RE.search(line)
        if m:
            task_id = m.group(1)
            tokens = m.group(2)
            state.active_tasks.pop(task_id, None)
            state.last_gen_milestone.pop(task_id, None)
            tid_cn = num_to_chinese(int(task_id))
            text = BroadcastTexts.pick("TASK_DONE", task_id=tid_cn, tokens=tokens)
            if text:
                tts_queue.put(text, priority=1, tag="task_done")
            continue

        # 空闲
        if TASK_IDLE_RE.search(line):
            if state.active_tasks:
                pass
            else:
                text = BroadcastTexts.pick("TASK_IDLE")
                if text:
                    tts_queue.put(text, priority=0, tag="idle")


def _check_hw_alerts(state, tts_queue):
    now = time.time()
    hw = get_hw_status()
    if hw:
        # GPU 温度
        if hw["gpu_temp"] >= GPU_TEMP_CRIT:
            _alert(tts_queue, state, "gpu_temp_crit", 3,
                   BroadcastTexts.pick("HW_GPU_TEMP_CRIT", temp=hw["gpu_temp"]))
        elif hw["gpu_temp"] >= GPU_TEMP_WARN:
            _alert(tts_queue, state, "gpu_temp_warn", 1,
                   BroadcastTexts.pick("HW_GPU_TEMP_WARN", temp=hw["gpu_temp"]))
        # CPU 温度
        if hw["cpu_temp"] >= CPU_TEMP_CRIT:
            _alert(tts_queue, state, "cpu_temp_crit", 3,
                   BroadcastTexts.pick("HW_CPU_TEMP_CRIT", temp=hw["cpu_temp"]))
        elif hw["cpu_temp"] >= CPU_TEMP_WARN:
            _alert(tts_queue, state, "cpu_temp_warn", 1,
                   BroadcastTexts.pick("HW_CPU_TEMP_WARN", temp=hw["cpu_temp"]))
    # 内存
    mem_pct = get_system_memory()
    if mem_pct is not None:
        if mem_pct >= MEM_CRIT_PCT:
            _alert(tts_queue, state, "mem_crit", 3,
                   BroadcastTexts.pick("HW_MEM_CRIT", pct=mem_pct))
        elif mem_pct >= MEM_WARN_PCT:
            _alert(tts_queue, state, "mem_warn", 1,
                   BroadcastTexts.pick("HW_MEM_WARN", pct=mem_pct))


def _check_router_errors(state, tts_queue):
    try:
        size = ROUTER_LOG.stat().st_size
    except Exception:
        return
    if size < state.router_error_offset:
        state.router_error_offset = 0
    if size == state.router_error_offset:
        return
    try:
        with open(ROUTER_LOG, "r") as f:
            f.seek(state.router_error_offset)
            new_lines = f.readlines()
            state.router_error_offset = f.tell()
    except Exception:
        return
    for line in new_lines:
        for pattern, alert_type, severity in ALERT_KEYWORDS:
            if pattern.search(line):
                text_map = {
                    "oom": "OOM", "xid_error": "XID_ERROR", "segfault": "SEGFAULT",
                    "vulkan_crash": "VULKAN_CRASH", "child_crash": "CHILD_CRASH",
                    "fatal": "FATAL", "error": "ERROR",
                }
                cat = text_map.get(alert_type, "ERROR")
                text = BroadcastTexts.pick(cat)
                if text:
                    _alert(tts_queue, state, alert_type, severity, text)
                break


def _check_daily_broadcast(state, tts_queue):
    now = time.time()
    if now - state.last_daily_broadcast >= DAILY_INTERVAL:
        state.last_daily_broadcast = now
        report = build_daily_report()
        if report:
            tts_queue.put(report, priority=0, tag="daily")
            logging.info(f"[Monitor] 日常播报: {report}")


def _alert(tts_queue, state, alert_type, severity, text):
    if not text:
        return
    now = time.time()
    # 严重告警去重
    if severity >= 3:
        last = state.alert_dedup.get(alert_type, 0)
        if now - last < ALERT_DEDUP:
            return
        state.alert_dedup[alert_type] = now
        tts_queue.put(text, priority=severity, tag=f"alert_{alert_type}")
        state.last_alert_time = now
        logging.info(f"[Monitor] 告警(sev={severity}): {text}")
    else:
        # 非严重告警全局冷却
        if now - state.last_alert_time < COOLDOWN:
            return
        tts_queue.put(text, priority=severity, tag=f"alert_{alert_type}")
        state.last_alert_time = now
        logging.info(f"[Monitor] 告警(sev={severity}): {text}")


# ============================================================
# 语音助手
# ============================================================

SESSION_TIMEOUT = 30  # 每次监听超时秒数

def run_voice_assistant(tts_queue):
    """语音助手主循环。

    两个业务：语音助手（VA）和监控播报。VA 优先。
    VA 会话期间 _voice_busy 置位，监控播报全部丢弃。

    状态机：
    [初始监听] VAD 运行 → 录音 → ASR → 唤醒词？
      ├─ 否 → quick_reply → 回到初始监听
      └─ 是 → 置位 _voice_busy（VAD 暂停、播报暂停）
              → 播放唤醒回复 → 等 3s
              → 进入会话循环：
                  [监听] VAD 录音（30s 超时）→ ASR
                    ├─ 超时（无声音）→ "再见" → 清 _voice_busy → 回到初始监听
                    ├─ ASR 为空 → "没听清，请再说一遍" → 等 3s → 回到 [监听]
                    └─ 有内容 → 暂停 VAD → LLM → TTS 播报 → 等 3s → 回到 [监听]
    """
    recorder = MicRecorder(listen_timeout=0)  # 初始监听：无超时，一直等唤醒词
    session_recorder = MicRecorder(
        silence_duration=SILENCE_DURATION_CMD,
        listen_timeout=SESSION_TIMEOUT,
    )
    history = []

    print("=" * 50)
    print("  AI 台灯语音助手（唤醒词模式）")
    print("  唤醒词: 你好小智 / 小智你好")
    print("  Ctrl+C 退出")
    print("=" * 50)

    while not _stop_event.is_set():
        # ── 初始监听：检测唤醒词 ──
        # 如果监控播报还在播放，等它结束
        if _tts_playing.is_set():
            tts_queue.wait_playing(timeout=60)
            time.sleep(3)

        wav = recorder.record_once()
        if not wav:
            continue

        user_text = transcribe(wav)
        if not user_text:
            time.sleep(3)
            continue

        print(f"\n听到: {user_text}")

        if not check_wake_word(user_text):
            # 未命中唤醒词，静默继续监听
            continue

        # ── 唤醒成功，进入 VA 会话 ──
        _voice_busy.set()
        print("[VA] 唤醒成功！进入会话")

        # 后台预热 LLM
        prewarm()

        # 播放唤醒回复（同步 TTS，内部设 _tts_playing）
        if WAKE_REPLY_PATH.exists():
            _play_wav_sync(WAKE_REPLY_PATH)
        else:
            _tts_speak_now(WAKE_REPLY)
        time.sleep(3)

        # 提取唤醒词后面的指令（如果有的话）
        command = extract_command(user_text)

        # ── 会话循环：监听 → ASR → LLM → TTS ──
        while not _stop_event.is_set():
            if command:
                # 唤醒词后面直接跟了指令，跳过监听
                pass
            else:
                # 监听用户提问（30s 超时）
                print(f"[VA] 请说话（{SESSION_TIMEOUT}s 内）...")
                wav = session_recorder.record_once()
                if not wav:
                    # 超时，没有声音
                    print("[VA] 超时，没有听到声音")
                    _tts_speak_now("再见")
                    time.sleep(3)
                    break

                user_text = transcribe(wav)
                if not user_text:
                    # 有声音但 ASR 为空
                    print("[VA] 没听清")
                    _tts_speak_now("没听清，请再说一遍")
                    time.sleep(3)
                    command = None  # 继续监听
                    continue

                print(f"\n听到: {user_text}")
                command = user_text

                # 检测结束对话
                if re.search(r'再见|拜拜|bye|走了|不聊了|先这样', command, re.IGNORECASE):
                    print("[VA] 用户说再见，结束对话")
                    _tts_speak_now("再见，有需要随时叫我")
                    time.sleep(3)
                    break

            # 此时 command 有内容
            print(f"你: {command}")

            # 快速回复，表示收到
            _play_quick_reply()

            # ASR→LLM 环节提示音
            _play_sound(SOUND_ASR_DONE)

            # 送 LLM
            reply = chat(command,
                         history=history if len(history) < 10 else history[-10:])
            if not reply:
                print("[VA] 思考失败")
                _tts_speak_now("我思考了一下，但出了点问题")
                time.sleep(3)
                command = None  # 继续监听
                continue

            # 保存对话历史
            history.append({"role": "user", "content": command})
            history.append({"role": "assistant", "content": reply})

            # LLM→TTS 环节提示音
            _play_sound(SOUND_LLM_DONE)

            # 播报 LLM 回复（同步 TTS）
            print(f"助手: {reply}")
            _tts_speak_now(reply)
            time.sleep(3)

            # 回到监听，等待下一个提问
            command = None
            print("\n" + "-" * 30)

        # ── 会话结束 ──
        _voice_busy.clear()
        print("[VA] 会话结束，回到监听")


def _play_wav_sync(wav_path):
    """同步播放 WAV（唤醒回复等需要等待的场景）。"""
    try:
        import wave
        with wave.open(str(wav_path), 'rb') as wf:
            duration = wf.getnframes() / wf.getframerate()
        timeout = max(int(duration + 5), 30)
        _tts_playing.set()
        subprocess.run(["aplay", "-q", "-D", "default", str(wav_path)],
                       check=True, capture_output=True, timeout=timeout)
    except Exception as e:
        print(f"[Play] 播放失败: {e}")
    finally:
        _tts_playing.clear()


# ============================================================
# 测试模式
# ============================================================

def run_calibrate():
    recorder = MicRecorder()
    threshold = recorder.calibrate()
    print(f"\n建议 SILENCE_THRESHOLD = {threshold}")


def run_test_mic():
    recorder = MicRecorder()
    wav = recorder.record_once()
    if wav:
        print(f"\n录音文件: {wav}")
    else:
        print("\n未录到语音")


def run_test_asr():
    recorder = MicRecorder()
    print("=== 录音 + ASR 测试 ===")
    wav = recorder.record_once()
    if not wav:
        print("未录到语音")
        return
    text = transcribe(wav)
    print(f"\n识别结果: {text}" if text else "\n识别失败")


def run_test_llm():
    recorder = MicRecorder()
    print("=== 录音 + ASR + LLM 测试 ===")
    wav = recorder.record_once()
    if not wav:
        print("未录到语音")
        return
    text = transcribe(wav)
    if not text:
        print("识别失败")
        return
    print(f"\n你: {text}")
    reply = chat(text)
    print(f"\n助手: {reply}" if reply else "\nLLM 请求失败")


def run_test_wake():
    recorder = MicRecorder()
    print("=== 唤醒词测试 ===")
    print("唤醒词: 你好小智 / 小智你好")
    wav = recorder.record_once()
    if not wav:
        print("未录到语音")
        return
    text = transcribe(wav)
    if not text:
        print("识别失败")
        return
    print(f"\n识别: {text}")
    if check_wake_word(text):
        cmd = extract_command(text)
        print(f"唤醒成功! 后续指令: '{cmd}'")
        prewarm()
        if WAKE_REPLY_PATH.exists():
            _play_wav_sync(WAKE_REPLY_PATH)
        else:
            tts_queue = TTSQueue()
            tts_queue.start()
            tts_queue.put(WAKE_REPLY, priority=2)
            tts_queue.wait_playing(timeout=30)
        if cmd:
            reply = chat(cmd)
            if reply:
                print(f"助手: {reply}")
    else:
        print("未检测到唤醒词")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AI 台灯 — 语音助手 + 监控播报")
    parser.add_argument("--monitor-only", action="store_true", help="仅监控播报")
    parser.add_argument("--voice-only", action="store_true", help="仅语音助手")
    parser.add_argument("--calibrate", action="store_true", help="校准麦克风")
    parser.add_argument("--test-mic", action="store_true", help="测试录音")
    parser.add_argument("--test-asr", action="store_true", help="测试录音+ASR")
    parser.add_argument("--test-llm", action="store_true", help="测试录音+ASR+LLM")
    parser.add_argument("--test-wake", action="store_true", help="测试唤醒词")
    parser.add_argument("--threshold", type=int, default=SILENCE_THRESHOLD, help="VAD 阈值")
    args = parser.parse_args()

    if args.calibrate:
        run_calibrate()
        return
    if args.test_mic:
        run_test_mic()
        return
    if args.test_asr:
        run_test_asr()
        return
    if args.test_llm:
        run_test_llm()
        return
    if args.test_wake:
        run_test_wake()
        return

    # 运行模式
    setup_logging()
    logging.info(f"[AI Station] 启动 (mode={'monitor' if args.monitor_only else 'voice' if args.voice_only else 'full'})")

    tts_queue = TTSQueue()
    tts_queue.start()

    monitor_thread = None
    if not args.voice_only:
        monitor_thread = threading.Thread(target=run_monitor, args=(tts_queue,),
                                          daemon=True, name="monitor")
        monitor_thread.start()
        logging.info("[AI Station] 监控线程已启动")

    if not args.monitor_only:
        try:
            run_voice_assistant(tts_queue)
        except KeyboardInterrupt:
            print("\n\n再见！")

    _stop_event.set()
    if monitor_thread:
        monitor_thread.join(timeout=5)

    logging.info("[AI Station] 已退出")


if __name__ == "__main__":
    main()
