#!/bin/bash
# Restart script for Daeng Bot services

echo "🔄 Daeng Bot Restart Script"
echo "=========================="

cd /root/daengbot

# Pull latest changes
echo "📥 Pulling latest changes from git..."
git pull origin main

# Restart all services
echo "🔄 Restarting systemd services..."
systemctl restart daengbot.service
systemctl restart daeng-callback.service
systemctl restart daeng-order-watcher.service

# Wait a moment for services to start
sleep 3

# Check status
echo ""
echo "📊 Service Status:"
echo "=================="

# Main bot status
echo "🤖 Main Bot:"
systemctl is-active daengbot.service && echo "✅ Running" || echo "❌ Failed"

# Callback server status
echo "🔔 Callback Server:"
systemctl is-active daeng-callback.service && echo "✅ Running" || echo "❌ Failed"

# Order watcher status
echo "👀 Order Watcher:"
systemctl is-active daeng-order-watcher.service && echo "✅ Running" || echo "❌ Failed"

echo ""
echo "📋 Detailed Status:"
echo "=================="
systemctl status daengbot.service --no-pager -l
echo ""
echo "---"
systemctl status daeng-callback.service --no-pager -l
echo ""
echo "---"
systemctl status daeng-order-watcher.service --no-pager -l

echo ""
echo "🎉 Restart complete!"
echo ""
echo "📝 To view logs:"
echo "  journalctl -u daengbot.service -f"
echo "  journalctl -u daeng-callback.service -f"
echo "  journalctl -u daeng-order-watcher.service -f"
