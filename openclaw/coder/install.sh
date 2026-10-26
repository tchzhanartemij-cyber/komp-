#!/bin/bash
# Очередь задач по коду для Джарвиса: служба jarvis-coder и навык coder.
#
# Запуск на сервере Джарвиса под jarvis:
#   bash ~/komp/openclaw/coder/install.sh
# Повторный запуск безопасен: обновит навык и перезапустит службу.
set -euo pipefail

if [ "$(id -un)" = "root" ]; then
  echo "Запусти под пользователем jarvis (su - jarvis), а не под root." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF="$HOME/.config/jarvis"
ENV_FILE="$CONF/coder.env"

if [ ! -s "$CONF/github-token" ]; then
  echo "Нет токена GitHub в $CONF/github-token. Сначала положи его туда." >&2
  exit 1
fi
CLAUDE_BIN="$(command -v claude || true)"
OPENCLAW_BIN="$(command -v openclaw || true)"
if [ -z "$CLAUDE_BIN" ] || [ -z "$OPENCLAW_BIN" ]; then
  echo "Не нашёл claude или openclaw в PATH. Запусти из обычного входа под jarvis." >&2
  exit 1
fi

echo "==> Куда слать отчёты в Telegram"
TG_TARGET="$(grep -s '^TG_TARGET=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "$TG_TARGET" ]; then
  TG_TARGET="$("$OPENCLAW_BIN" config get channels.telegram.allowFrom 2>/dev/null | grep -oE '[0-9]{5,}' | head -1 || true)"
fi
if [ -n "$TG_TARGET" ]; then
  echo "   твой Telegram ID: $TG_TARGET"
else
  echo "   Не нашёл твой Telegram ID в настройках Джарвиса."
  echo "   Узнай его у бота @userinfobot (пришлёт число Id) и введи сюда."
  read -rp "Telegram ID: " TG_TARGET
  [[ "$TG_TARGET" =~ ^[0-9]{5,}$ ]] || { echo "Это не похоже на ID (нужно число)." >&2; exit 1; }
fi

umask 077
mkdir -p "$CONF"
cat > "$ENV_FILE" <<EOF
TG_TARGET=$TG_TARGET
CLAUDE_BIN=$CLAUDE_BIN
OPENCLAW_BIN=$OPENCLAW_BIN
EOF
umask 022

echo "==> Проверяю доступ к GitHub"
python3 - "$CONF/github-token" <<'PY'
import json, sys, urllib.request
token = open(sys.argv[1]).read().strip()
req = urllib.request.Request("https://api.github.com/repos/tchzhanartemij-cyber/komp-",
                             headers={"Authorization": f"Bearer {token}"})
repo = json.load(urllib.request.urlopen(req, timeout=20))
if not repo.get("permissions", {}).get("push"):
    sys.exit("   токен видит komp-, но не может в него писать: нужно Contents: Read and write")
print(f"   {repo['full_name']}: запись разрешена")
PY

echo "==> Навык для Джарвиса"
mkdir -p "$HOME/.openclaw/workspace/skills/coder"
sed -e "s#__CODER__#$HERE/coder.py#g" -e "s#__FRAMES__#$HERE/frames.sh#g" -e "s#__WATCH__#$HERE/watch.py#g" \
  "$HERE/SKILL.md" > "$HOME/.openclaw/workspace/skills/coder/SKILL.md"
if [ -s "$CONF/gemini-key" ]; then
  echo "   ключ Gemini есть: видео смотрит Gemini"
else
  echo "   нет ключа Gemini ($CONF/gemini-key): видео будут разбираться по кадрам"
fi
if command -v ffmpeg >/dev/null; then
  echo "   ffmpeg есть: видео-образцы будут раскладываться на кадры"
else
  echo "   НЕТ ffmpeg: без него видео-образцы не разложить на кадры."
  echo "   Поставь под root (exit из jarvis): apt install -y ffmpeg"
fi

echo "==> Служба jarvis-coder"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/jarvis-coder.service" <<EOF
[Unit]
Description=Jarvis coding task queue
After=network-online.target

[Service]
Environment=PATH=$(dirname "$CLAUDE_BIN"):$(dirname "$OPENCLAW_BIN"):/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $HERE/coder.py worker
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable jarvis-coder.service >/dev/null
systemctl --user restart jarvis-coder.service
sleep 2
systemctl --user is-active --quiet jarvis-coder.service && echo "   служба работает"

echo "==> Пробное сообщение в Telegram"
if "$OPENCLAW_BIN" message send --channel telegram --target "$TG_TARGET" \
     --message "Очередь задач по коду готова. Пиши задачи для komp- в любое время." ; then
  echo "   отправлено, проверь Telegram"
else
  echo "   НЕ отправилось. Пришли этот экран." >&2
fi

echo
echo "Готово. Перезапусти Джарвиса, чтобы он увидел навык: openclaw gateway restart"
