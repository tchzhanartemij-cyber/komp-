#!/bin/bash
# Потолки памяти для служб Джарвиса, чтобы одна разросшаяся служба не вешала весь сервер.
#
# Запуск под root:
#   bash /home/jarvis/komp/openclaw/limits/apply.sh
#
# MemoryHigh — выше этого служба тормозится и уходит в подкачку.
# MemoryMax  — жёсткий потолок: выше система убьёт процесс внутри этой службы,
#              а не SSH, Telegram или «Сводку». Служба поднимется сама (Restart=always).
#              Джарвис и очередь задач при этом не останавливаются (OOMPolicy=continue):
#              гибнет только разросшийся процесс, например Кодекс или браузер.
# Сторож jarvis-memwatch предупреждает в Telegram на 80% потолка и о каждом таком случае.
# Повторный запуск безопасен. Службы не перезапускаются, потолки применяются на ходу.
set -euo pipefail

if [ "$(id -u)" != 0 ]; then
  echo "Запусти под root." >&2
  exit 1
fi

USER_NAME=jarvis
USER_HOME="/home/$USER_NAME"
USER_UNITS="$USER_HOME/.config/systemd/user"

# служба | MemoryHigh | MemoryMax | при нехватке: continue — убить процесс, служба живёт;
#                                        stop — перезапустить службу целиком
# Джарвис запускает внутри себя Claude, Кодекс и браузер, поэтому ему больше всех.
USER_LIMITS="
openclaw-gateway|4G|5G|continue
jarvis-coder|1500M|2G|continue
cloudcli|512M|768M|stop
freellmapi|512M|768M|stop
jarvis-limits|256M|384M|stop
"
# «Сводка» сама запускает агента, ему нужен запас.
SYSTEM_LIMITS="
life-dashboard|1500M|2G|continue
"

write_dropin() {   # каталог служб, имя, high, max, oom-политика
  local dir="$1/$2.service.d"
  mkdir -p "$dir"
  cat > "$dir/memory.conf" <<CONF
# Создано openclaw/limits/apply.sh
[Service]
MemoryAccounting=yes
MemoryHigh=$3
MemoryMax=$4
OOMPolicy=$5
CONF
}

echo "==> Службы jarvis"
for line in $USER_LIMITS; do
  IFS='|' read -r name high max oom <<<"$line"
  if [ -f "$USER_UNITS/$name.service" ]; then
    write_dropin "$USER_UNITS" "$name" "$high" "$max" "$oom"
    echo "   $name: тормоз $high, потолок $max"
  fi
done

echo "==> Сторож памяти (предупреждения в Telegram)"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cat > "$USER_UNITS/jarvis-memwatch.service" <<UNIT
[Unit]
Description=Jarvis memory watcher

[Service]
Environment=PATH=$USER_HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $HERE/watch.py
Restart=always
RestartSec=30
MemoryMax=128M

[Install]
WantedBy=default.target
UNIT
chown -R "$USER_NAME:$USER_NAME" "$USER_UNITS"

echo "==> Системные службы"
for line in $SYSTEM_LIMITS; do
  IFS='|' read -r name high max oom <<<"$line"
  if [ -f "/etc/systemd/system/$name.service" ]; then
    write_dropin /etc/systemd/system "$name" "$high" "$max" "$oom"
    echo "   $name: тормоз $high, потолок $max"
  fi
done

systemctl daemon-reload
UID_J="$(id -u "$USER_NAME")"
sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$UID_J" systemctl --user daemon-reload
sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$UID_J" systemctl --user enable --now jarvis-memwatch.service >/dev/null 2>&1
sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$UID_J" systemctl --user restart jarvis-memwatch.service

echo
echo "==> Проверка: сколько ест сейчас / потолок"
show() {  # имя, затем вывод systemctl show: MemoryCurrent и MemoryMax по строке
  local name="$1" cur max
  { read -r cur; read -r max; } <<<"$2"
  [[ "$cur" =~ ^[0-9]+$ ]] && cur="$((cur / 1048576)) МБ" || cur="—"
  [[ "$max" =~ ^[0-9]+$ ]] && max="$((max / 1048576)) МБ" || max="нет"
  printf "   %-18s %8s / %s\n" "$name" "$cur" "$max"
}
for line in $USER_LIMITS; do
  IFS='|' read -r name _ _ <<<"$line"
  [ -f "$USER_UNITS/$name.service" ] || continue
  show "$name" "$(sudo -u "$USER_NAME" XDG_RUNTIME_DIR="/run/user/$UID_J" systemctl --user show "$name" -p MemoryCurrent -p MemoryMax --value)"
done
for line in $SYSTEM_LIMITS; do
  IFS='|' read -r name _ _ <<<"$line"
  [ -f "/etc/systemd/system/$name.service" ] || continue
  show "$name" "$(systemctl show "$name" -p MemoryCurrent -p MemoryMax --value)"
done
echo
echo "Если у службы потолок «нет», пришли этот экран."
