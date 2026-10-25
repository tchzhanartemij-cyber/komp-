#!/bin/bash
# Ставит «Пульт жизни» на сервер рядом с OpenClaw:
#   - дашборд как службу systemd (слушает только 127.0.0.1:8787);
#   - Caddy: HTTPS-сертификат и вход по паролю, адрес вида https://1-2-3-4.sslip.io.
# Запуск (под пользователем, от имени которого работает OpenClaw):
#   bash ~/openclaw/dashboard/install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/life-dashboard"
LIFE_DIR="${LIFE_DIR:-$HOME/.openclaw/workspace/life}"
RUN_USER="$(id -un)"

if [ "$RUN_USER" = "root" ]; then
  echo "Запусти скрипт под пользователем OpenClaw (например, jarvis), а не под root." >&2
  exit 1
fi

read -rp "Логин для входа на дашборд [jarvis]: " LOGIN
LOGIN="${LOGIN:-jarvis}"
while true; do
  read -rsp "Придумай пароль для дашборда (от 10 символов): " PASS; echo
  [ "${#PASS}" -ge 10 ] && break
  echo "Слишком короткий, нужно от 10 символов."
done
read -rp "Часовой пояс [Europe/Moscow]: " TZONE
TZONE="${TZONE:-Europe/Moscow}"

echo "==> Ставлю Python и Caddy"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 curl gpg debian-keyring debian-archive-keyring apt-transport-https
if ! sudo apt-get install -y -qq caddy; then
  # Нет в репозитории системы: ставим из официального репозитория Caddy.
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy
fi

echo "==> Часовой пояс: $TZONE"
sudo timedatectl set-timezone "$TZONE"

echo "==> Копирую дашборд в $APP_DIR, данные в $LIFE_DIR"
mkdir -p "$APP_DIR" "$LIFE_DIR"
cp "$SRC_DIR/server.py" "$SRC_DIR/index.html" "$APP_DIR/"

echo "==> Служба life-dashboard"
sudo tee /etc/systemd/system/life-dashboard.service >/dev/null <<EOF
[Unit]
Description=Life dashboard
After=network.target

[Service]
User=$RUN_USER
Environment=LIFE_DIR=$LIFE_DIR
Environment=DASHBOARD_HOST=127.0.0.1
Environment=DASHBOARD_PORT=8787
ExecStart=/usr/bin/python3 -I $APP_DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now life-dashboard
sudo systemctl restart life-dashboard

echo "==> Адрес и HTTPS"
IP="$(curl -4 -fsS https://api.ipify.org)"
DOMAIN="${IP//./-}.sslip.io"
HASH="$(caddy hash-password --plaintext "$PASS")"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
	basicauth {
		$LOGIN $HASH
	}
	reverse_proxy 127.0.0.1:8787
}
EOF
sudo systemctl enable caddy >/dev/null
sudo systemctl restart caddy

if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 80/tcp >/dev/null
  sudo ufw allow 443/tcp >/dev/null
fi

sleep 3
if curl -fsS -o /dev/null http://127.0.0.1:8787/api/data; then
  echo
  echo "Готово. Дашборд: https://$DOMAIN"
  echo "Логин: $LOGIN, пароль — тот, что ты ввёл."
  echo "Первое открытие может занять до минуты, пока Caddy получает сертификат."
else
  echo "Дашборд не отвечает. Посмотри журнал: sudo journalctl -u life-dashboard -n 50" >&2
  exit 1
fi
