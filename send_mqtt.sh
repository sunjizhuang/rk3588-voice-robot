#!/bin/bash

# MQTT配置
MQTT_HOST="192.168.200.30"
MQTT_PORT="1883"
MQTT_TOPIC="device/command"

# 从命令行参数获取JSON，或使用默认值
if [ $# -eq 1 ]; then
    JSON_MSG="$1"
else
    # 默认JSON
    JSON_MSG='{"cmd":"mode","mode":"stand","device":"lite3"}'
fi

echo "准备向$MQTT_HOST发送 $MQTT_TOPIC : $JSON_MSG"

# 调用mosquitto_pub发送
timeout 2 mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t "$MQTT_TOPIC" -m "$JSON_MSG"

if [ $? -eq 0 ]; then
    echo "发送成功"
else
    echo "发送失败"
    exit 1
fi
