#!/bin/sh
# Один запуск делает два дела (нужен sudo):
#  1) «Сводка» открывается по личной ссылке и работает как приложение на главном экране без пароля от браузера;
#  2) окно с Claude и другими ИИ доступно по адресу ai.138-124-113-106.sslip.io (логин jarvis и тот же пароль).
# Старый Caddyfile сохраняется копией; если что-то не так, скрипт сам откатывается.
set -e
HOST=138-124-113-106.sslip.io
CF=${CADDYFILE:-/etc/caddy/Caddyfile}
TOK=$HOME/.config/jarvis/dashboard-token
HASH=$(awk '$1=="jarvis" {print $2; exit}' "$CF")
[ -n "$HASH" ] || { echo "не нашёл хэш пароля в $CF, ничего не менял"; exit 1; }

mkdir -p "$HOME/.config/jarvis"
if [ ! -s "$TOK" ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(32),end='')" > "$TOK"
  chmod 600 "$TOK"
fi

NEW=$(mktemp)
cat > "$NEW" <<CADDY
$HOST {
	reverse_proxy 127.0.0.1:8787
}

ai.$HOST {
	basicauth {
		jarvis $HASH
	}
	reverse_proxy 127.0.0.1:3001
}
CADDY

if [ -n "$DRY_RUN" ]; then echo "--- новый Caddyfile ---"; sed 's/\$2a.*/<хэш>/' "$NEW"; exit 0; fi

BAK="$CF.bak-$(date +%F-%H%M)"
sudo cp "$CF" "$BAK"
echo "копия старой настройки: $BAK"
sudo systemctl restart life-dashboard
sleep 2
CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/ || true)
[ "$CODE" = "401" ] || { echo "«Сводка» не перешла на вход по ссылке (код $CODE), ничего не менял в Caddy"; exit 1; }
sudo cp "$NEW" "$CF"
if sudo caddy validate --config "$CF" >/dev/null 2>&1 && sudo systemctl reload caddy; then
  echo "готово."
  echo "Личная ссылка на «Сводку» (не показывай никому): https://$HOST/?k=$(cat "$TOK")"
  echo "Окно с Claude: https://ai.$HOST (логин jarvis, пароль как раньше)"
else
  sudo cp "$BAK" "$CF"; sudo systemctl reload caddy || true
  echo "не получилось, вернул старую настройку Caddy"; exit 1
fi
