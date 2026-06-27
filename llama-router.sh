#!/bin/bash
# Qwen3.6 LLM Router Service (systemd compatible - foreground)

LOGDIR="/home/zxw/logs/llama"
BINDIR="/home/zxw/llama.cpp/build/bin"
ROUTER="$BINDIR/llama-server"

export LD_LIBRARY_PATH="$BINDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Vulkan: 限制每批提交节点数，避免 APU GPU job timeout (ErrorDeviceLost)
export GGML_VK_MAX_NODES_PER_SUBMIT=1

mkdir -p "$LOGDIR"

exec "$ROUTER" \
  --host 127.0.0.1 \
  --port 12345 \
  --api-key 71769f2CeCE681015e1B71eCf848900e \
  -a Qwen3.6 \
  --models-dir /home/zxw/model \
  --models-max 1 \
  --models-preset /home/zxw/model/router-preset.ini \
  --metrics \
  >> "$LOGDIR/router.log" 2>&1
