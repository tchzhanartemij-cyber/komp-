#!/bin/bash
# Выход из режима входа: браузер Джарвиса снова работает невидимо, входы сохранены.
set -euo pipefail
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
echo "==> Возвращаю браузер Джарвиса в обычный режим (перезапуск 2–3 минуты)"
openclaw browser stop >/dev/null 2>&1 || true
openclaw config set browser.headless true --strict-json
systemctl --user unset-environment DISPLAY
openclaw gateway restart
bash "$(dirname "$0")/stop.sh"
echo "Готово. Входы сохранены, Джарвис может пользоваться аккаунтами."
