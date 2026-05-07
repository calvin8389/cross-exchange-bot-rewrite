#!/usr/bin/env bash
# close_all.sh — 触发 bot 平掉所有持仓（保持进程运行）
# 用法: ./close_all.sh

PID=$(pgrep -f "src.main" | head -1)

if [ -z "$PID" ]; then
  echo "❌ Bot 进程未找到（src.main 未运行）"
  exit 1
fi

echo "✅ Bot PID=$PID，发送 SIGUSR1 → 触发 CLOSING..."
kill -USR1 "$PID"
echo "   等待平仓完成，监控日志："
echo "   tail -f bot_errors.log"
echo "   或: grep 'CLOSED\|ERROR\|CLOSING' bot.log | tail -10"
