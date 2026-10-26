#!/bin/bash
# Выключает временный удалённый экран. Входы в аккаунты сохраняются.
for p in "websockify.*6080" "x11vnc -display :1" "fluxbox" "Xvfb :1"; do
  pkill -u "$(id -u)" -f "$p" 2>/dev/null || true
done
echo "Экран выключен."
