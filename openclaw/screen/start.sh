#!/bin/bash
# Временный удалённый экран сервера: нужен, чтобы войти в аккаунты (Instagram и т.п.)
# в окне браузера. Открывается только через туннель Termius, в интернет не торчит.
#
#   bash ~/openclaw/screen/start.sh            — включить экран
#   bash ~/openclaw/screen/start.sh instagram  — включить экран и открыть вход в Instagram
#   bash ~/openclaw/screen/stop.sh             — выключить экран
set -euo pipefail

if ! command -v Xvfb >/dev/null || ! command -v x11vnc >/dev/null || ! command -v websockify >/dev/null; then
  echo "==> Ставлю программы для экрана (один раз, попросит пароль jarvis)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq xvfb x11vnc novnc websockify fluxbox
fi

RUN="$HOME/.screen-run"
mkdir -p "$RUN"
export DISPLAY=:1

if ! pgrep -u "$(id -u)" -f "Xvfb :1" >/dev/null; then
  nohup Xvfb :1 -screen 0 1280x800x24 -nolisten tcp >"$RUN/xvfb.log" 2>&1 &
  sleep 2
  nohup fluxbox >"$RUN/fluxbox.log" 2>&1 &
fi
if ! pgrep -u "$(id -u)" -f "x11vnc -display :1" >/dev/null; then
  # Только localhost: снаружи к экрану не подключиться, только через туннель Termius.
  nohup x11vnc -display :1 -localhost -nopw -forever -shared -rfbport 5901 -quiet >"$RUN/x11vnc.log" 2>&1 &
fi
if ! pgrep -u "$(id -u)" -f "websockify.*6080" >/dev/null; then
  nohup websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5901 >"$RUN/websockify.log" 2>&1 &
fi
sleep 2

echo
echo "Экран включён."
echo "1. В Termius добавь проброс порта (как для 18789): Local, порт 6080 -> 127.0.0.1:6080, хост nd."
echo "2. Открой в Safari:  http://127.0.0.1:6080/vnc.html  и нажми «Connect»."

if [ "${1:-}" != "" ]; then
  echo
  echo "==> Открываю вход: $1. Окно появится на экране, войди в аккаунт сам."
  agent-reach login "$1" || true
  echo "Когда вошёл, проверь: agent-reach check $1 --text"
fi
echo "Когда закончишь: bash ~/openclaw/screen/stop.sh"
