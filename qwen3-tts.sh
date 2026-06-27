#!/bin/bash
# Qwen3-TTS Server (systemd compatible - foreground)
# Model: Qwen3-TTS-12Hz-0.6B-CustomVoice (9 preset speakers)

LOGDIR="/home/zxw/logs/tts"
BINDIR="/home/zxw/qwen3-tts/build/bin"
TTS="$BINDIR/qwen_tts"
MODEL_DIR="/home/zxw/model-tts/Qwen3-TTS-12Hz-0.6B-CustomVoice"
PORT=12348
THREADS=8

mkdir -p "$LOGDIR"

exec "$TTS" --serve "$PORT" -d "$MODEL_DIR" -j "$THREADS" -S >> "$LOGDIR/tts.log" 2>&1
