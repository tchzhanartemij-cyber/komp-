#!/bin/bash
# Режим входа в аккаунты: браузер Джарвиса показывается на удалённом экране,
# ты сам входишь в нужные сайты, входы сохраняются в профиле браузера Джарвиса.
#
#   bash ~/openclaw/screen/login.sh avito instagram yandex ozon gmail wb
#   (можно и адресами: bash ~/openclaw/screen/login.sh https://example.com)
# Когда вошёл везде:  bash ~/openclaw/screen/login-done.sh
set -euo pipefail
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

declare -A SITES=(
  [avito]=https://www.avito.ru/profile
  [instagram]=https://www.instagram.com/accounts/login/
  [yandex]=https://passport.yandex.ru/auth
  [ozon]=https://www.ozon.ru/my/main
  [wb]=https://www.wildberries.ru/security/login
  [gmail]=https://accounts.google.com/
  [vk]=https://vk.com/
  [x]=https://x.com/login
  [hh]=https://hh.ru/account/login
)

bash "$(dirname "$0")/start.sh"

echo "==> Включаю видимый браузер Джарвиса на удалённом экране (перезапуск займёт 2–3 минуты)"
openclaw browser stop >/dev/null 2>&1 || true
systemctl --user set-environment DISPLAY=:1
openclaw config set browser.headless false --strict-json
openclaw gateway restart
openclaw browser start

for s in "$@"; do
  url="${SITES[$s]:-$s}"
  echo "   открываю $url"
  openclaw browser open "$url" || echo "   не открылось: $url"
done

echo
echo "Теперь в Safari (http://127.0.0.1:6080/vnc.html) войди во все открытые сайты сам."
echo "Коды из SMS и почты вводи там же. Пароли никому не присылай."
echo "Когда закончишь:  bash ~/openclaw/screen/login-done.sh"
