#!/bin/bash
# Выкладка «Сводки» из этого репозитория на боевую службу life-dashboard.
# Запуск: sudo bash /home/jarvis/komp/openclaw/dashboard/deploy-live.sh
# (или от jarvis без sudo, если разрешён перезапуск:
#   jarvis ALL=(root) NOPASSWD: /usr/bin/systemctl restart life-dashboard)
# Делает резервную копию, кладёт файлы, перезапускает службу и откатывает всё, если она не поднялась.
set -uo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
APP=/home/jarvis/life-dashboard
FILES="server.py agent.py office.py workspace.py attachments.py talk_helper.mjs index.html icon.png"
B=/home/jarvis/backups/life-dashboard-$(date +%Y%m%d-%H%M%S)
OWN=(); [ "$(id -u)" = 0 ] && OWN=(-o jarvis -g jarvis)
restart() { if [ "$(id -u)" = 0 ]; then systemctl restart life-dashboard; else sudo -n /usr/bin/systemctl restart life-dashboard; fi; }
healthy() {
  sleep 4
  systemctl is-active --quiet life-dashboard || return 1
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/)" = 401 ]
}

if [ "${1:-}" = --rollback ]; then
  [ -d "${2:-}" ] || { echo "Укажи папку копии"; exit 1; }
  for f in $FILES; do if [ -e "$2/$f" ]; then cp -p "$2/$f" "$APP/$f"; else rm -f "$APP/$f"; fi; done
  restart && healthy && echo "ОТКАЧЕНО к $2" || { echo "Служба не отвечает после отката"; exit 1; }
  exit 0
fi

mkdir -p "$B"; [ "$(id -u)" = 0 ] && chown jarvis:jarvis "$B"
for f in $FILES; do [ -e "$APP/$f" ] && cp -p "$APP/$f" "$B/"; done
echo "Копия: $B"
for f in $FILES; do install "${OWN[@]}" -m 0644 "$SRC/$f" "$APP/$f" || { echo "Не удалось положить $f"; exit 1; }; done
if restart && healthy; then
  echo "ВЫКЛАДКА УСПЕШНА ($(git -c safe.directory="*" -C "$SRC" rev-parse --short HEAD)). Откат: sudo bash $SRC/deploy-live.sh --rollback $B"
  exit 0
fi
echo "==> Служба не поднялась, ОТКАТ"
journalctl -u life-dashboard -n 20 --no-pager 2>/dev/null
for f in $FILES; do if [ -e "$B/$f" ]; then cp -p "$B/$f" "$APP/$f"; else rm -f "$APP/$f"; fi; done
restart && healthy && echo "ВЫКЛАДКА ОТКАЧЕНА" || echo "ВНИМАНИЕ: после отката служба не отвечает"
exit 1
