# Evidence: задача №10, срез 1 (2026-10-28)

| Что | Тип | Результат |
|---|---|---|
| `python3 -m unittest discover -s tests` | mock (поддельный claude-бинарь, синтетические логи/очередь) | 41 OK, 0 skipped |
| `node tests/qa_frontend.cjs`, `node tests/office_reducer.cjs` | mock | passed |
| `preview.json` | preview: `python3 -I server.py`, отдельный порт 8795, временные LIFE_DIR/токен/очередь кодера с «секретами» в text/result/session/prompt | `/api/team` без токена и с неверным токеном → 401; с токеном 200; утечек 0 (team и office); outcome неизвестного статуса → «неизвестно»; фактическая модель руководителя из init-строки лога |
| `live-model-proof.json` | live: тот же изолированный preview, реальный HOME только для авторизации CLI; 3 запроса «Ответь одним словом» через `/api/ai/send` с cookie+Origin | запрошено opus → фактически `claude-opus-5-5`; sonnet → `claude-sonnet-5-5`; haiku → `claude-haiku-5-5`; mismatch=false; proof записан в `ai/model-proof.json` временного LIFE_DIR |
| UI-снимки desktop/iPad/phone | **не сделаны** | браузера (Chromium/Playwright) в окружении этого воркера нет; managed browser из №8 недоступен. Нужен отдельный browser-прогон |
| Физическое устройство / production | **не выполнялось** | деплоя, рестарта сервисов, merge не было |

Важно: живой прогон доказывает только **чат без инструментов** (`claude -p --tools ''`). Это не агент
и не маршрутизация `/v1/responses`. Руководитель кодера по-прежнему запускается с моделью по
умолчанию CLI; его фактическая модель берётся из лога запуска.

## Модель исполнителя и ревью
- Код среза написан через `codex exec -s workspace-write` (startup: `model: gpt-6.1-sol`,
  `reasoning effort: none`, codex-cli 0.164.0). Astra/max per invocation не запрашивался; глобальные
  настройки Codex не менялись.
- Дифф Codex проверен руководителем (Claude Code, Opus 5.5) построчно; исправлено руками:
  потеря offline-fallback офиса при некорректном снимке, кэш разбора логов по (путь, mtime, размер)
  чтобы не перечитывать ~320 КиБ × 100 задач на каждом опросе, предупреждение о подмене модели в
  proof селектора. Независимый Opus-рецензент отдельным запуском **не выполнялся** (blocked: в раннере
  нет роли рецензента, см. brief Ф2).
- `codex_runs` считает вызовы `codex exec`, видимые в первых 64 КиБ и последних 256 КиБ лога: для
  длинных логов это нижняя граница («замечено»), не точное число.
