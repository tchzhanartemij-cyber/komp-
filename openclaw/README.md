# Джарвис на OpenClaw: траты и дела голосом в Telegram

Пишешь или говоришь боту в Telegram «потратил 500 на такси» или «надо купить подарок маме до четверга». Он записывает трату или дело, а на вопросы «сколько я потратил в октябре?» и «что по делам?» отвечает сводкой.

Как это устроено:
- **Сервер (VPS):** на нём круглосуточно работает OpenClaw.
- **Claude:** мозг ассистента.
- **Groq:** расшифровывает голосовые сообщения. У него есть бесплатный тариф.
- **Скил `life-ledger` из этой папки:** учит ассистента вести траты и дела.

Инструкция собрана по официальной документации OpenClaw (github.com/openclaw/openclaw, октябрь 2026).

---

## Что понадобится

| Что | Где взять | Сколько стоит |
|---|---|---|
| VPS на Ubuntu 24.04, от 2 ГБ памяти | Hetzner, DigitalOcean, Timeweb и т.п. | ~$5–10 в месяц |
| Ключ Claude API | console.anthropic.com → API Keys | оплата по факту, для личного учёта это немного |
| Ключ Groq для голосовых | console.groq.com → API Keys | есть бесплатный тариф |
| Telegram-бот | @BotFather в Telegram | бесплатно |

Ключи никому не показывай и не присылай в чат, даже мне. Вставляй их только на сервере.

---

## Шаг 1. Зайди на сервер

После покупки VPS тебе дадут IP-адрес и пароль от `root`. На Windows открой PowerShell и введи:

```bash
ssh root@ТВОЙ_IP
```

Создай отдельного пользователя. OpenClaw выполняет команды на сервере, и от имени `root` запускать его опасно:

```bash
adduser jarvis
usermod -aG sudo jarvis
su - jarvis
```

Все следующие команды выполняй под пользователем `jarvis`.

## Шаг 2. Установи OpenClaw

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
```

Установщик сам поставит нужную версию Node.js и запустит мастер настройки. В мастере:
- **модель (provider):** выбери **Anthropic API key** и вставь ключ Claude;
- **запуск в фоне (daemon):** соглашайся, тогда ассистент будет работать постоянно.

Если мастер не запустился сам:

```bash
openclaw onboard --install-daemon
```

Проверь, что всё работает:

```bash
openclaw gateway status
```

## Шаг 3. Подключи Telegram

1. В Telegram открой **@BotFather** (проверь, что ник именно такой), отправь `/newbot`, придумай имя и сохрани **токен**, который он выдаст.
2. На сервере выполни:

   ```bash
   openclaw channels add --channel telegram --token ТОКЕН_ОТ_BOTFATHER
   openclaw channels status --probe
   ```

3. Напиши своему боту в Telegram любое сообщение. Потом на сервере:

   ```bash
   openclaw pairing list telegram
   openclaw pairing approve telegram КОД_ИЗ_СПИСКА
   ```

   Код действует 1 час. После подтверждения бот отвечает только тебе, а чужие сообщения ждут твоего подтверждения.

## Шаг 4. Включи голосовые

Открой файл настроек:

```bash
nano ~/.openclaw/openclaw.json
```

Добавь внутрь главных фигурных скобок блок с ключом Groq (если блок `env` уже есть, допиши `GROQ_API_KEY` в него):

```json5
env: { vars: { GROQ_API_KEY: "gsk_ТВОЙ_КЛЮЧ" } },
```

Сохрани файл (Ctrl+O, Enter, Ctrl+X) и перезапусти ассистента:

```bash
openclaw gateway restart
```

Если бот не понимает голосовые, проверь, видит ли он Groq:

```bash
openclaw capability audio providers
```

## Шаг 5. Поставь скил трат и дел

```bash
mkdir -p ~/.openclaw/workspace/skills/life-ledger
curl -fsSL https://raw.githubusercontent.com/tchzhanartemij-cyber/komp-/claude/eager-mendel-oncysb/openclaw/skills/life-ledger/SKILL.md \
  -o ~/.openclaw/workspace/skills/life-ledger/SKILL.md
```

Репозиторий `komp-` приватный, поэтому эта ссылка без входа в GitHub не откроется. Если `curl` выдаст ошибку, создай файл вручную: выполни `nano ~/.openclaw/workspace/skills/life-ledger/SKILL.md`, вставь туда содержимое `skills/life-ledger/SKILL.md` из этой папки и сохрани.

Если в шаге 2 мастер выбрал другую рабочую папку (workspace), положи скил в `<твоя рабочая папка>/skills/life-ledger/`. Посмотреть её можно в `~/.openclaw/openclaw.json`.

## Шаг 6. Проверь

Напиши боту или скажи голосом:
- «потратил 450 на продукты»
- «надо оплатить интернет до пятницы»
- «сколько я потратил за октябрь?»
- «что по делам?»
- «интернет оплатил»

Траты записываются в `~/.openclaw/workspace/life/expenses.csv`, дела в `~/.openclaw/workspace/life/tasks.md`.

---

## Если что-то не работает

- Бот молчит: `openclaw channels status --probe` и `openclaw logs --follow`.
- Ошибка «unauthorized token»: токен скопирован не полностью. Возьми новый у @BotFather (`/token`).
- Общая диагностика: `openclaw doctor`.

## Безопасность

- Не открывай веб-панель OpenClaw (Control UI) в интернет без пароля. Смотри её через `openclaw dashboard` на сервере или по SSH-туннелю.
- Не подтверждай в `pairing` чужие коды.
- Обновляйся время от времени: `openclaw update` (или повторный запуск установщика).
