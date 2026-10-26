#!/bin/bash
# Московский прокси для браузера Джарвиса.
#
# Запуск на сервере Джарвиса под пользователем jarvis:
#   bash ~/openclaw/msk-proxy/setup.sh
#
# Что делает:
#   1. Создаёт на сервере Джарвиса отдельный ключ для туннеля.
#   2. Один раз заходит на московский сервер под root (спросит пароль) и заводит там
#      пользователя tunnel, которому можно только пропускать трафик: без входа в систему,
#      без файлов, без команд.
#   3. Держит постоянный зашифрованный туннель: 127.0.0.1:1080 на сервере Джарвиса
#      выходит в интернет с московского адреса. Оборвётся — переподключится сам.
#   4. Пускает через туннель только браузер Джарвиса. Claude, Telegram и Agent Reach
#      продолжают ходить напрямую.
set -euo pipefail

if [ "$(id -un)" = "root" ]; then
  echo "Запусти под пользователем jarvis (su - jarvis), а не под root." >&2
  exit 1
fi

read -rp "IP московского сервера: " MSK
if ! [[ "$MSK" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "Это не похоже на IP-адрес (нужно вида 123.45.67.89)." >&2
  exit 1
fi

KEY="$HOME/.ssh/msk_tunnel"
PORT=1080
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "jarvis-msk-tunnel" -f "$KEY"
fi
PUB="$(cat "$KEY.pub")"

echo
echo "==> Настраиваю московский сервер."
echo "    Сейчас спросит пароль root от МОСКОВСКОГО сервера. Символы не видны, это нормально."
ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no "root@$MSK" "PUB='$PUB' bash -s" <<'REMOTE'
set -euo pipefail
id tunnel >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin tunnel
install -d -m 700 -o tunnel -g tunnel /home/tunnel/.ssh
printf 'restrict,port-forwarding %s\n' "$PUB" > /home/tunnel/.ssh/authorized_keys
chown tunnel:tunnel /home/tunnel/.ssh/authorized_keys
chmod 600 /home/tunnel/.ssh/authorized_keys
cat > /etc/ssh/sshd_config.d/10-tunnel.conf <<'EOF'
# Пользователь tunnel: только пропуск трафика для браузера Джарвиса.
Match User tunnel
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding local
    PermitTunnel no
    GatewayPorts no
    ForceCommand /usr/sbin/nologin
Match all
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
echo "   московский сервер готов"
REMOTE

echo "==> Проверяю туннель"
ssh -i "$KEY" -o BatchMode=yes -o ExitOnForwardFailure=yes -N -D "127.0.0.1:$PORT" "tunnel@$MSK" &
TEST_PID=$!
sleep 5
COUNTRY="$(curl -s --max-time 20 --socks5-hostname "127.0.0.1:$PORT" https://ipinfo.io/country || true)"
kill "$TEST_PID" 2>/dev/null || true
wait "$TEST_PID" 2>/dev/null || true
if [ "$COUNTRY" != "RU" ]; then
  echo "Туннель не заработал (страна: '${COUNTRY:-нет ответа}'). Пришли этот экран." >&2
  exit 1
fi
echo "   через туннель видна страна: $COUNTRY"

echo "==> Служба туннеля (переподключается сама)"
# После `su - jarvis` нет связи с systemd пользователя: указываем её явно.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/msk-tunnel.service" <<EOF
[Unit]
Description=Moscow SOCKS tunnel for Jarvis browser
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -i $KEY -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -N -D 127.0.0.1:$PORT tunnel@$MSK
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now msk-tunnel.service
sleep 5
COUNTRY="$(curl -s --max-time 20 --socks5-hostname "127.0.0.1:$PORT" https://ipinfo.io/country || true)"
echo "   служба работает, страна: ${COUNTRY:-нет ответа}"

echo "==> Браузер Джарвиса: российские сайты через Москву, остальное напрямую"
bash "$(dirname "$0")/ru-sites.sh"

echo
echo "Готово. Перезапусти Джарвиса: openclaw gateway restart"
echo "Потом напиши ему: «открой в браузере ipinfo.io и скажи, какая там страна»."
