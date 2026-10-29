# Исправления QA — независимая проверка 2026-10-28

Ветка fix/svodka-qa-20261028, база e424e11, сохранены embedded agent и office. Кодовые commits: 2feb760, 3bca56b, 656495e. Production не менялся и не перезапускался.

## Результат
Все исходные R0–R6 исправлены и имеют executable regression coverage. Дополнительно устранены найденные при review: пустую панель можно снова открыть, созданная страница выбирается, save не ждет медленного sync, ошибки dismissible и привязаны к path+payload, неоднозначные network/502/503/504 не предлагают blind retry. Browser выявил ошибку видимости feedback за native modal backdrop: исправлено, inline role=alert теперь внутри формы. Installer source теперь копирует agent.py; installer НЕ запускался, systemd/config не менялись.

## Независимые автоматические тесты
- python3 -m unittest discover -s openclaw/dashboard/tests -v: **26/26 passed, 0 skips**, включая все исходные 21 tests. Полный output: evidence/final-tests.txt.
- Node tests/qa_frontend.cjs: R1–R6, все восемь common forms, 48 rejected/ambiguous cases, latest edits, draft/feedback isolation, 12 blur refresh, single-flight stale invalidation, abort/backoff, no dangling promises.
- JS syntax: node --check extracted inline scripts через test_javascript_syntax; passed.
- Exact startup: python3 -I /absolute/worktree/openclaw/dashboard/server.py с disposable HOME/LIFE_DIR/token/port, hostile cwd/PYTHONPATH, disabled agent: real HTTP 401/auth/data/agent/office passed. Тест trusted loader также проверяет dataclass registration и отказ от symlink наружу.
- Дополнительно фактический QA server запущен ровно /usr/bin/python3 -I <worktree>/openclaw/dashboard/server.py на отдельном порту 8793. Python isolation не ослаблялась.
- git diff --check passed.

## Реальный managed Chromium, только synthetic data
Одна вкладка/один browser, без parallel Codex/Claude. Worker и review закончены до browser. Не было live prompts, оплат/внешних model calls, production writes. Авторизация disposable token; ни token, ни auth URL не сохраняются в evidence.

1. **Формы 48/48 passed:** восемь типов x HTTP400/401/409/500, offline, proxy503. Открытый диалог, весь draft сохранен, видимый inline alert внутри формы; rejected mutation не выглядит как подтвержденное сохранение. Network/proxy ambiguity без автоматического retry. Успешный retry закрывает unchanged форму/очищает fields и feedback; запись подтверждена в synthetic data.
2. **Pending edits + duplicate submit:** один POST на два requestSubmit; текст, введенный после отправки, сохранен в открытой форме; исходный отправленный текст появился в data. Нет молчаливой потери нового текста.
3. **Memory desktop 1440x1000:** X действительно hidden=true, граф расширился **774 -> 1148px**; renderPanel/graphShown не открывают закрытую панель. Факты не удаляются при close.
4. **Mobile390x820:** выбранная карточка теперь y≈0, получает focus; native Escape скрывает. Ранее карточка начиналась y917. Скриншот final-memory-mobile.png.
5. **Tablet820x1180:** selected panel top436, внутри viewport; горизонтального overflow нет. Desktop overflow также нет.
6. **Memory drafts/error:** 12 blur refresh, переключение QA Person/QA Project — отдельные drafts и error feedback сохраняются, чужая ошибка не протекает. После успешного retry dynamic form feedback действительно очищается. Создание QA Created In Browser автоматически выбирает новую страницу.
7. **Пустой граф:** nodes=0, close и внешняя кнопка «Новая страница» снова открывают форму; reload не нужен.
8. **Slow network:** browser задержка13s -> maxActive(data GET)=1, same Promise, pending0. Сохранение во время delayed700ms GET завершилось за7ms; старый snapshot отброшен, второй последовательный GET подтвердил свежую note, maxActive1.
9. **Реальный timeout20s:** AbortController сработал через20001ms, calls1, flightCleared, backoff=true; load(true) после снятия fault восстановил связь.
10. Отдельный bounded navigation soak 60 переходов по шести разделам, **30.002s**; DOM неизменен682, heap2.78→2.72MB, JS errors/unhandled rejections0, pendingData=false; итоговый JSON в evidence/browser-final.json. Это не 10h endurance claim. Исходный audit до исправлений дополнительно имел 80 переходов/123s и candidate60/42s.

## Review
Claude Sonnet выполнен последовательно, замечания первой проверки исправлены. Два итоговых review не нашли blocker/high; тексты включены рядом. Medium предположение о старом inline feedback после rebuild проверено реальным browser retry: successFeedbackCleared=true. Backend idempotency для неоднозначных сетевых разрывов не вводилась — вместо опасного retry пользователь видит необходимость проверить результат. Drafts живут в памяти вкладки, не переживают reload (это не было исходным требованием).

## Cleanup
QA server на8793 остановлен, порт больше не слушает. Browser остановлен ради RAM. Одноразовый токен уничтожен. Production PID144287 и HTML SHA256 a2eebcd1a554ef7c75799fa196f4cf6282dade86475761ffad927e537d5fceaf не изменились. Скриншоты просмотрены, только синтетические QA имена, без credentials.

## Выкладка / blocker
Production сохраняет task7, процесс /usr/bin/python3 -I /home/jarvis/life-dashboard/server.py. Для безопасной выкладки требуется подтвержденный доступ к штатному service restart/rollback; существующий sudo blocker не обходился. Service не убивался, gateway/config не менялись. Простой file copy при уже работающем старом Python не считается безопасной выкладкой: возможен mixed old backend/new frontend. Родителю передается готовая ветка для проверки; deploy только отдельным решением и при доступном штатном restart+rollback.
