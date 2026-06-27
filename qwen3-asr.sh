#!/bin/bash
# ASR 纯 CPU 推理（不占用 GPU，避免影响大模型）
# ctx-size 65536 = 模型完整训练上下文
# --no-cache-idle-slots: ASR不需要KV cache复用，关闭节约内存
# --cache-ram 0: 关闭 prompt cache，ASR 每次独立推理无需缓存
# --parallel 1: 单并发，ASR 单用户场景

LOGDIR="/home/zxw/logs/llama"
BINDIR="/home/zxw/llama.cpp/build/bin"
SERVER="$BINDIR/llama-server"
MODEL="/home/zxw/model-asr/Qwen3-ASR-1.7B-Q8_0.gguf"
MMPROJ="/home/zxw/mmproj/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"
PORT=12347

export LD_LIBRARY_PATH="$BINDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

mkdir -p "$LOGDIR"

exec "$SERVER" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --model "$MODEL" \
  --mmproj "$MMPROJ" \
  --ctx-size 65536 \
  --n-gpu-layers 0 \
  --threads 8 \
  --parallel 1 \
  --mlock \
  --no-cache-idle-slots \
  --cache-ram 0 \
  --timeout 600 \
  >> "$LOGDIR/asr.log" 2>&1
