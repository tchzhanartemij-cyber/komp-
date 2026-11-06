# Джарвис и «Сводка»: заметки для Claude Code

Владелец: Артемий. Общаться по-русски, просто и коротко, без жаргона. Он работает с iPad
(Termius, Safari, Telegram). Секреты (токены, ключи, пароли) никогда не просить в чат и не
коммитить: он вводит их сам скрытым вводом (`read -rsp`).

## Карта репозитория

- `openclaw/dashboard/` — «Сводка»: `server.py` (сервер), `index.html` (весь интерфейс), `agent.py`,
  `office.py` (офис агентов), `workspace.py` (диалог Telegram), `attachments.py`, `talk_helper.mjs`
  (живой голос), `tests/`, `deploy-live.sh` (выкладка), `SPEC.md` (главное ТЗ).
- `openclaw/coder/` — очередь задач по коду `jarvis-coder` (`coder.py`), скилл Джарвиса `SKILL.md`.
- `openclaw/limits/`, `openclaw/msk-proxy/`, `openclaw/screen/`, `openclaw/skills/` — служебное: потолки памяти, туннель через Москву, экран, скиллы Джарвиса; доступы — `openclaw/ACCESS.md`.
- Общая память агентов — вне репозитория: `~/.openclaw/workspace/SHARED.md` (см. там).

## Сервер

Aeza NL, `new-coral`, 138.124.113.106, Ubuntu 24.04, 2 ядра, 8 ГБ RAM, подкачка 4 ГБ.
Время сервера Asia/Irkutsk (время владельца). Работать под пользователем `jarvis`.
Root владелец вводит сам; команды под root давать ему текстом.

- `~/komp` — этот репозиторий. Основная ветка `claude/eager-mendel-oncysb`.
- Службы jarvis (`systemctl --user`): `openclaw-gateway` (Джарвис, бот @Mnchb21bot
  в Telegram, внутри него Claude CLI, Кодекс и браузер), `jarvis-coder` (очередь задач по коду),
  `jarvis-memwatch` (сторож памяти), `msk-tunnel` (SOCKS через Москву на 127.0.0.1:1080),
  `cloudcli`, `freellmapi`, `jarvis-limits` (поставил сам Джарвис).
- Системные: `life-dashboard` («Сводка» на 127.0.0.1:8787), `caddy` (https://138-124-113-106.sslip.io).
  Боевая «Сводка» работает из `/home/jarvis/life-dashboard`, а не из `~/komp`; как она туда
  выкладывается, смотреть в `ExecStart` службы и в `~/backups/life-dashboard-predeploy-*/deploy.sh`.
- Данные владельца: `~/.openclaw/workspace/life/` (траты, еда, дела, память). Не портить:
  перед изменением формата делать копию.
- Потолки памяти на службы: `openclaw/limits/apply.sh` (под root). Сервер уже вешался от
  нехватки памяти: тяжёлое (сборки, браузер, Кодекс) не запускать параллельно.

## Правила

- `openclaw gateway restart` и перезапуск `life-dashboard` обрывают работу у владельца
  (Telegram, открытая «Сводка»). Предупреждать перед этим.
- Выкладка на боевую «Сводку»: с 06.11.2026 Claude выкладывает сам после тестов —
  `bash openclaw/dashboard/deploy-live.sh` (копия и откат встроены; право только на перезапуск
  службы — `/etc/sudoers.d/jarvis-svodka`). После выкладки — итог и команда отката владельцу.
- Необратимые действия в интернете (покупки, отправка сообщений от его имени) только после «да».
- Задачи по коду, включая «Сводку», идут по цепочке: Джарвис ставит их в очередь кодера
  (скилл `coder`), Claude Code делает в ветке `jarvis/task-N` и отдаёт объёмный код Codex.
  Прямые правки из отдельной сессии Claude Code — только по прямой просьбе владельца и только
  когда в очереди нет незавершённой задачи по тем же файлам (`python3 openclaw/coder/coder.py list`):
  иначе две стороны правят одни файлы.

## Состояние (02.11.2026)

Работает (выложено 06.11.2026; выкладка — `bash openclaw/dashboard/deploy-live.sh`):
- «Сводка»: меню из 5 разделов, кнопка Джарвиса; чаты в «ИИ» идут через Джарвиса (сессия
  `agent:main:svodka-<чат>`, инструменты и общая память, GPT-6.1 Sol или Claude Opus); живой голос —
  OpenClaw Talk (GPT-Live по подписке ChatGPT); офис агентов с живой лентой; память из Mem разложена по темам.
- Джарвис: модель по умолчанию GPT-6.1 Sol (Claude в его режиме без API-ключа не работает).
- Очередь кодера: код пишет тот из Claude Code и Codex, кто меньше израсходовал подписку; в Telegram
  роли и итог, «да» после отчёта = принять; старт/финиш пишутся в общий дневник.
- Общая память агентов: `~/.openclaw/workspace/SHARED.md`, `MEMORY.md`, `memory/`. Скиллы общие,
  включая build-your-harness (проверка: `python3 ~/.agents/skills/build-your-harness/better-your-harness/scripts/scan.py <папка>`).

Сделано 04–06.11 (всё на боевом): шрифт Onest, спокойный дизайн без градиента; вкладки «Чаты / Проекты / Код»;
чаты Claude Code и Codex в «Чатах» (`allchats.py`, только чтение); шаги Джарвиса в реальном времени и раскрытие
«что выполнил / что получил» (`activity.py` читает журнал сессии из `~/.openclaw/agents/main/agent/openclaw-agent.sqlite`);
диктовка → промпт (`/api/ai/prompt`, Sonnet); лимиты Claude и ChatGPT в выборе модели; телефон: чат привязан к видимой
области, меню прячется при клавиатуре. Heartbeat Джарвиса выключен (`every: 0m`) — он съедал подписку ChatGPT.
Скиллы через `find-skills`: frontend-design, mobile-native, web-design-guidelines (в `~/.agents/skills`, у всех агентов).

Ждёт владельца: перезапуск очереди (`sudo -u jarvis XDG_RUNTIME_DIR=/run/user/$(id -u jarvis) systemctl --user restart jarvis-coder`);
автоматизации Джарвиса «Дневник дня» и «Разбор недели» (`openclaw cron add`, защита Claude Code не даёт
их создать); проверка голоса на iPhone; решение по задачам №10 и №17.

Дальше по плану: утренняя сводка в 9:00 в Telegram; доступ Джарвиса к CRM monotonica.online;
сжать `~/.openclaw/workspace/AGENTS.md` (11 КБ на каждый ответ); почта и календарь; перенос памяти из ChatGPT.

Экономия лимита: длинная сессия Claude Code перечитывает всю переписку на каждом шаге (дошло до
885 тыс. токенов на шаг). Большие этапы — в новой сессии; состояние держать здесь и в общей памяти.
