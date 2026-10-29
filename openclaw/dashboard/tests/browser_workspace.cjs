#!/usr/bin/env node
'use strict';
// Disposable integration fixture; no operator data or credentials.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os');
const http = require('node:http'), assert = require('node:assert/strict'), { spawn } = require('node:child_process');
const dashboard = path.resolve(__dirname, '..'), evidence = path.join(dashboard, 'docs/evidence/task12');
const engine = process.argv[2] || 'chromium', token = 'synthetic-workspace-agent-token', key = 'agent:svodka-test:main';
if (process.argv.length > 3 || !['chromium', 'webkit'].includes(engine)) { console.error('Usage: node tests/browser_workspace.cjs [chromium|webkit]'); process.exit(2); }
const clean = x => String(x).replaceAll(token, '[redacted]').replace(/Bearer\s+[^\s"\\]+/gi, 'Bearer [redacted]').replace(/([?&])k=[^\s"'<>]*/gi, '[redacted-query]');
const delay = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, message, timeout = 25000) { const end = Date.now() + timeout; while (Date.now() < end) { const v = await fn(); if (v) return v; await delay(50); } throw Error(message); }
async function listen(s) { await new Promise((r, j) => { s.once('error', j); s.listen(0, '127.0.0.1', r); }); return s.address().port; }
async function close(s) { if (s) { s.closeAllConnections(); await new Promise(r => s.close(r)); } }
(async () => {
  await fs.mkdir(evidence, { recursive: true });
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'svodka-workspace-')), oldTmp = process.env.TMPDIR;
  process.env.TMPDIR = root;
  const report = { engine, environment: 'viewport emulation; synthetic Gateway', checks: [], pageErrors: [], consoleErrors: [], screenshots: [] };
  const requests = [], transcript = Array.from({ length: 45 }, (_, i) => ({ role: i === 2 ? 'system' : i % 2 ? 'assistant' : 'user', content: `Синтетическое сообщение ${i}`, timestamp: 1700000000000 + i * 1000, __openclaw: { id: `initial-${i}` } }));
  transcript[20].content += ' <think>секрет</think>';
  transcript[21].content += ' sk-синтетическийключ1234567890';
  transcript[22].content = [{ type: 'text', text: 'Синтетическое вложение' }, { type: 'image', name: 'fixture.png' }];
  let offline = false, gateway, child, browser, context, page, origin, serverLog = '';
  async function check(name, fn) { const start = Date.now(); try { const details = await fn(); report.checks.push({ name, passed: true, err: null, ms: Date.now() - start, details }); } catch (e) { report.checks.push({ name, passed: false, err: clean(e.stack || e), ms: Date.now() - start }); process.exitCode = 1; } console.log(`${engine}: ${name}: ${report.checks.at(-1).passed ? 'passed' : 'FAILED'}`); }
  async function api(p, route, body, headers = {}) { return p.evaluate(async ({ route, body, headers }) => { const r = await fetch(route, { ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json', ...headers } }) }); return { status: r.status, body: await r.json() }; }, { route, body, headers }); }
  const texts = p => p.locator('#chat-messages article > p').allTextContents();
  const countText = async (p, text) => (await texts(p)).filter(t => t === text).length;
  async function unique(p, expected) { const values = await texts(p); assert.equal(values.length, expected); assert.equal(new Set(values).size, expected); }
  function observe(p) { p.on('pageerror', e => report.pageErrors.push(clean(e.message))); p.on('console', m => { if (m.type() === 'error') report.consoleErrors.push(clean(m.text())); }); }
  async function boot(p) { await p.goto(origin + '/#chat'); await p.waitForFunction(() => window.__svodkaBooted === true); await until(async () => (await p.locator('#chat-live').innerText()).includes('обновлено'), 'No updated status'); }
  try {
    await fs.mkdir(path.join(root, 'life')); await fs.writeFile(path.join(root, 'agent-token'), token, { mode: 0o600 });
    assert.equal((await fs.stat(path.join(root, 'agent-token'))).mode & 0o777, 0o600);
    gateway = http.createServer(async (req, res) => {
      try {
        let raw = ''; for await (const chunk of req) raw += chunk;
        if (offline) { res.writeHead(503).end('offline'); return; }
        const body = JSON.parse(raw || '{}');
        const json = payload => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: true, result: { details: payload } })); };
        if (req.url === '/tools/invoke' && req.method === 'POST') {
          if (body.tool === 'sessions_list') return json({ sessions: [{ key: 'main', kind: 'main', channel: 'telegram', status: 'running' }, { key: 'agent:svodka-test:subagent:x', kind: 'other', parentSessionKey: 'main', label: 'Субагент-тест', status: 'running' }, { key: 'agent:svodka-test:telegram:group:1', kind: 'group', channel: 'telegram' }] });
          if (body.tool === 'sessions_history') { const { offset = 0, limit = 30 } = body.args; const end = Math.max(0, transcript.length - offset), start = Math.max(0, end - limit); return json({ messages: transcript.slice(start, end), offset, limit, nextOffset: offset + end - start, hasMore: start > 0 }); }
        }
        if (req.url !== '/v1/responses' || req.method !== 'POST') { res.writeHead(404).end(); return; }
        const record = { headers: req.headers, body }; requests.push(record);
        const n = requests.length, text = body.input;
        transcript.push({ role: 'user', content: text, timestamp: Date.now(), __openclaw: { id: `sent-${n}` } });
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
        const emit = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
        emit('response.created', { response: { model: 'fake-model' } }); await delay(1200);
        record.answer = `Синтетический ответ ${n}`;
        emit('response.output_text.delta', { delta: record.answer });
        transcript.push({ role: 'assistant', content: record.answer, timestamp: Date.now(), __openclaw: { id: `answer-${n}` } });
        emit('response.completed', { response: { model: 'fake-model', status: 'completed' } }); res.end();
      } catch (e) { res.destroy(); }
    });
    const gwPort = await listen(gateway), reservation = http.createServer(), port = await listen(reservation); await close(reservation); origin = `http://127.0.0.1:${port}`;
    const env = { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: root, TMPDIR: root, XDG_CONFIG_HOME: root, XDG_CACHE_HOME: root };
    child = spawn('/usr/bin/python3', ['-I', '-B', path.join(dashboard, 'server.py')], { cwd: root, detached: true, env: { ...env, LIFE_DIR: path.join(root, 'life'), DASHBOARD_HOST: '127.0.0.1', DASHBOARD_PORT: String(port), DASHBOARD_TOKEN_FILE: path.join(root, 'absent-token'), SVODKA_AGENT_TRUSTED_PROXY: '1', SVODKA_AGENT_ENABLED: '1', SVODKA_WORKSPACE_ENABLED: '1', SVODKA_CANONICAL_SESSION: key, SVODKA_AGENT_ID: 'svodka-test', SVODKA_AGENT_URL: `http://127.0.0.1:${gwPort}/v1/responses`, SVODKA_AGENT_TOKEN_FILE: path.join(root, 'agent-token') }, stdio: ['ignore', 'pipe', 'pipe'] });
    child.stdout.on('data', b => { serverLog = (serverLog + b).slice(-12000); }); child.stderr.on('data', b => { serverLog = (serverLog + b).slice(-12000); });
    let childError; child.on('error', e => { childError = e; });
    await until(async () => { if (childError || child.exitCode !== null) throw Error(clean(childError || serverLog)); try { return (await fetch(origin + '/api/workspace')).ok; } catch { return false; } }, 'Server did not start');
    const pw = require('/home/jarvis/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
    try { browser = await pw[engine].launch({ headless: true, env, ...(engine === 'chromium' ? { executablePath: '/opt/google/chrome/chrome' } : {}) }); }
    catch (e) { if (engine !== 'webkit') throw e; report.skipped = true; report.skipReason = clean(e.stack || e); console.log('webkit: skipped: ' + clean(e.message)); return; }
    report.browserVersion = browser.version(); context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    page = await context.newPage(); observe(page);
    await check('boot', async () => { await boot(page); assert.equal(await page.locator('#chat-key').innerText(), key); assert.deepEqual(report.pageErrors, []); assert.deepEqual(report.consoleErrors, []); });
    await check('history', async () => {
      await unique(page, 30); await page.locator('#chat-earlier').click(); await until(async () => (await texts(page)).length === 44, 'Earlier page absent'); await unique(page, 44);
      const content = await page.locator('#chat-messages').innerText(); assert(!/think|секрет|синтетическийключ|Синтетическое сообщение 2\n/.test(content)); assert(content.includes('[image: fixture.png]'));
      const tasks = await page.locator('#chat-tasks').innerText(); assert(tasks.includes('Субагент-тест')); assert(!tasks.includes('telegram:group')); return { initial: 30, totalVisible: 44, systemHidden: true };
    });
    const text = 'Синтетическая проверка отправки workspace'; let sent;
    await check('send and duplicate', async () => {
      await page.locator('#chat-input').fill(text); sent = await page.evaluate(() => JSON.parse(sessionStorage.getItem('svodka-chat')));
      const start = Date.now(); await page.locator('#chat-send').click();
      await until(async () => await countText(page, text) === 1, 'Sent message absent'); const messageMs = Date.now() - start;
      await until(async () => requests[0]?.answer && await countText(page, requests[0].answer) === 1, 'Assistant absent');
      report.latency = { mock: true, label: 'synthetic Gateway polling latency', messageMs, assistantMs: Date.now() - start };
      assert.equal(requests.length, 1); assert.equal(requests[0].headers['x-openclaw-session-key'], key); assert(!Object.hasOwn(requests[0].body, 'user')); assert(requests[0].body.input.startsWith(`[Сводка:${sent.request}]`));
      assert((await page.locator('#chat-messages article').filter({ has: page.locator('p', { hasText: text }) }).innerText()).includes('Сводка'));
      assert(!(await page.locator('#chat-messages').innerText()).includes('ожидает подтверждения'));
      assert.equal((await api(page, '/api/workspace/send', sent)).body.duplicate, true); await delay(500); assert.equal(requests.length, 1);
    });
    await check('two tabs', async () => { const second = await context.newPage(); observe(second); try { await boot(second); for (const p of [page, second]) { assert.equal(await countText(p, text), 1); assert.equal(await countText(p, requests[0].answer), 1); } await second.locator('#chat-earlier').click(); await until(async () => (await texts(second)).length === (await texts(page)).length, 'Tabs did not converge'); assert.deepEqual(await texts(second), await texts(page)); } finally { await second.close(); } });
    await check('revision and duplicate', async () => {
      const card = page.locator('#chat-messages article').filter({ has: page.locator('p').filter({ hasText: text }) }); await card.getByRole('button', { name: 'Уточнить', exact: true }).click();
      await page.locator('#chat-revision textarea').nth(1).fill('Уточнённая синтетическая формулировка');
      const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/workspace/revision') && r.request().method() === 'POST');
      await page.getByRole('button', { name: 'Сохранить черновик', exact: true }).click(); const draft = await (await responsePromise).json();
      await delay(1500); assert.equal(requests.length, 1);
      await page.getByRole('button', { name: 'Отправить изменённый запрос', exact: true }).click(); await until(() => requests.length === 2, 'Revision not submitted');
      assert(requests[1].body.input.includes(text)); assert(requests[1].body.input.includes('Новая редакция')); assert.equal(await countText(page, text), 1);
      assert.equal((await api(page, '/api/workspace/revision/send', { id: draft.id, confirm: true })).body.duplicate, true);
      await until(async () => requests[1].answer && await countText(page, requests[1].answer) === 1, 'Revision answer absent'); assert.equal(requests.length, 2);
    });
    await check('offline recovery', async () => { offline = true; try { await until(async () => (await page.locator('#chat-live').innerText()).includes('соединение потеряно'), 'Offline not reported'); } finally { offline = false; } await until(async () => (await page.locator('#chat-live').innerText()).includes('обновлено'), 'Recovery absent', 40000); await unique(page, 48); });
    await check('cancel disabled with reason', async () => { const buttons = page.locator('#chat-tasks button'); assert(await buttons.count() >= 2); for (const b of await buttons.all()) { assert(await b.isDisabled()); assert((await b.getAttribute('title')).length > 15); } });
    await check('security', async () => {
      assert.equal((await api(page, '/api/workspace/send', sent, { 'Content-Type': 'text/plain' })).status, 415);
      const status = await new Promise((resolve, reject) => { const req = http.request(origin + '/api/workspace/send', { method: 'POST', headers: { Origin: 'http://evil.test', 'Content-Type': 'application/json' } }, res => { res.resume(); res.on('end', () => resolve(res.statusCode)); }); req.on('error', reject); req.end(JSON.stringify(sent)); }); assert.equal(status, 403);
      assert(!(await page.content()).includes(token)); for (const route of ['/', '/api/workspace', '/api/workspace/history', '/api/agent']) { const body = await (await fetch(origin + route)).text(); assert(!body.includes(token), route); } assert(!serverLog.includes(token));
    });
    await check('navigation and voice regression', async () => { for (const tab of ['life', 'work', 'graph', 'ai', 'agents', 'chat']) { await page.locator('#tab-' + tab).click(); await page.locator('#page-' + tab).waitFor({ state: 'visible' }); } await page.locator('#voice-btn').click(); await page.locator('#voice').waitFor({ state: 'visible' }); await page.locator('#voice-close').click(); assert.deepEqual(report.pageErrors, []); });
    for (const [label, width, height] of [['desktop', 1440, 1000], ['ipad', 820, 1180], ['phone', 390, 844]]) await check('screenshot ' + label, async () => { await page.setViewportSize({ width, height }); await page.locator('#tab-chat').click(); const file = `${engine}-${label}.png`; await page.screenshot({ path: path.join(evidence, file), animations: 'disabled' }); report.screenshots.push(file); });
  } catch (e) { await check('setup / suite', async () => { throw e; }); }
  finally {
    await context?.close(); await browser?.close();
    if (child?.pid) { try { process.kill(-child.pid, 'SIGTERM'); } catch {} await Promise.race([new Promise(r => child.exitCode !== null || child.signalCode ? r() : child.once('exit', r)), delay(2000)]); try { process.kill(-child.pid, 'SIGKILL'); } catch {} }
    await close(gateway); report.gateway = { count: requests.length, requests }; report.serverLog = clean(serverLog); report.ok = !report.skipped && report.checks.every(c => c.passed);
    await fs.writeFile(path.join(evidence, `browser-${engine}.json`), clean(JSON.stringify(report, null, 2)) + '\n'); await fs.rm(root, { recursive: true, force: true }); if (oldTmp === undefined) delete process.env.TMPDIR; else process.env.TMPDIR = oldTmp;
  }
})().catch(e => { console.error(clean(e.stack || e)); process.exitCode = 1; });
