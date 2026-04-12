#!/bin/bash
# Restart script for Daeng Bot

cd /root/daengbot

echo "Pulling latest changes..."
git pull origin main

echo "Stopping running processes..."
pkill -f "telegram_daeng_all_in_one_bot_v5.py"
pkill -f "daeng_callback_server.py"
pkill -f "daeng_order_watcher.py"

sleep 2

echo "Starting processes..."
nohup /root/daengbot/venv/bin/python /root/daengbot/telegram_daeng_all_in_one_bot_v5.py > bot.log 2>&1 &
nohup /root/daengbot/venv/bin/python /root/daengbot/daeng_callback_server.py > callback.log 2>&1 &
nohup /root/daengbot/venv/bin/python /root/daengbot/daeng_order_watcher.py > watcher.log 2>&1 &

sleep 2

echo "Verifying processes..."
ps aux | grep -E "telegram_daeng|daeng_callback|daeng_order_watcher" | grep -v grep

echo "Done! Check logs if needed:"
echo "  tail -f bot.log"
echo "  tail -f callback.log"
echo "  tail -f watcher.log"
