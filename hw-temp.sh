#!/bin/bash
# 硬件温度/负载综合日志
# 记录 GPU(amdgpu) / CPU(k10temp) / NVMe / 网卡 等温度 + GPU 负载

INTERVAL=60
LOGFILE="/home/zxw/logs/hw-temp.log"
mkdir -p "$(dirname "$LOGFILE")"

echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] hw-temp started" >> "$LOGFILE"

while true; do
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    
    # GPU 负载
    busy=$(cat /sys/class/drm/card*/device/gpu_busy_percent 2>/dev/null | head -1)
    
    # 各传感器温度
    gpu_edge=$(cat /sys/class/hwmon/hwmon7/temp1_input 2>/dev/null)
    cpu_tctl=$(cat /sys/class/hwmon/hwmon4/temp1_input 2>/dev/null)
    nvme=$(cat /sys/class/hwmon/hwmon2/temp1_input 2>/dev/null)
    nic_r8169=$(cat /sys/class/hwmon/hwmon3/temp1_input 2>/dev/null)
    nic_eno1=$(cat /sys/class/hwmon/hwmon5/temp1_input 2>/dev/null)
    wifi=$(cat /sys/class/hwmon/hwmon6/temp1_input 2>/dev/null)
    
    # 转换为 °C
    gpu_edge=${gpu_edge:+$((gpu_edge / 1000))}
    cpu_tctl=${cpu_tctl:+$((cpu_tctl / 1000))}
    nvme=${nvme:+$((nvme / 1000))}
    nic_r8169=${nic_r8169:+$((nic_r8169 / 1000))}
    nic_eno1=${nic_eno1:+$((nic_eno1 / 1000))}
    wifi=${wifi:+$((wifi / 1000))}
    
    echo "$ts gpu_busy=${busy:-0}% gpu=${gpu_edge:-NA}°C cpu=${cpu_tctl:-NA}°C nvme=${nvme:-NA}°C r8169=${nic_r8169:-NA}°C eno1=${nic_eno1:-NA}°C wifi=${wifi:-NA}°C" >> "$LOGFILE"
    sleep "$INTERVAL"
done
