#!/usr/bin/env node
'use strict';
// Operator-run integration test. No production configuration, credentials, or data are used.
// Usage: node tests/browser_voice.cjs [chromium|webkit|all]
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os');
const http = require('node:http'), assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const dashboard = path.resolve(__dirname, '..'), evidence = path.join(os.tmpdir(), 'svodka-evidence-voice');
const choice = process.argv[2] || 'all', engines = choice === 'all' ? ['chromium', 'webkit'] : [choice];
if (process.argv.length > 3 || engines.some(e => !['chromium', 'webkit'].includes(e))) {
  console.error('Usage: node tests/browser_voice.cjs [chromium|webkit|all]'); process.exit(2);
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const clean = x => String(x).replaceAll('synthetic-voice-test-token', '[redacted]')
  .replace(/Bearer\s+\S+/gi, 'Bearer [redacted]').replace(/([?&])k=[^\s"'<>]*/gi, '[redacted-query]');
async function until(fn, message, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const result = await fn(); if (result) return result; await delay(50); }
  throw new Error(message);
}
async function listen(server) {
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return server.address().port;
}
async function closeServer(server) {
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
function wav() {
  const rate = 16000, samples = rate * 32, b = Buffer.alloc(44 + samples * 2);
  b.write('RIFF'); b.writeUInt32LE(b.length - 8, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(rate, 24); b.writeUInt32LE(rate * 2, 28);
  b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(samples * 2, 40);
  let seed = 17;
  for (let i = 0; i < samples; i++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const signal = i % 51200 < 19200 ? .42 * Math.sin(2 * Math.PI * 210 * i / rate) + .28 * (seed / 2 ** 32 - .5) : 0;
    b.writeInt16LE(Math.round(signal * 32767), 44 + i * 2);
  }
  return b; // Ten 1.2 s speech-level bursts / 2 s silence; Chromium loops by default.
}
function browserMocks({ denied }) {
  window.__spoken = []; window.__spokenCancelled = 0; window.__cancelledTexts = []; window.__tracks = [];
  window.__states = []; window.__notices = [];
  let active = null, timer = null;
  const synth = { getVoices: () => [{ lang: 'ru-RU', name: 'MOCKED TTS' }], resume() {}, pause() {},
    get speaking() { return !!active; }, get pending() { return false; }, get paused() { return false; },
    speak(u) {
      if (!u.text) { setTimeout(() => { u.onstart?.({}); u.onend?.({}); }, 0); return; }
      active = u; window.__spoken.push(u.text);
      timer = setTimeout(() => { if (active !== u) return; u.onstart?.({});
        timer = setTimeout(() => { if (active === u) { active = null; u.onend?.({}); } }, Math.min(3000, u.text.length * 40));
      }, 0);
    },
    cancel() { clearTimeout(timer); const u = active; active = null;
      if (u) { window.__spokenCancelled++; window.__cancelledTexts.push(u.text);
        if (u.onerror) u.onerror({ error: 'canceled' }); else u.onend?.({}); }
    }, addEventListener() {}, removeEventListener() {} };
  Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: synth });
  Object.defineProperty(window, 'SpeechSynthesisUtterance', { configurable: true,
    value: class { constructor(text = '') { this.text = text; } } });
  if (navigator.mediaDevices) {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async options => {
      if (denied) throw new DOMException('Synthetic permission denial', 'NotAllowedError');
      const stream = await original(options); window.__tracks.push(...stream.getTracks()); return stream;
    };
  }
  setInterval(() => {
    const state = document.querySelector('#voice')?.dataset.state, notice = document.querySelector('#voice-notice')?.textContent;
    if (state && window.__states.at(-1) !== state) window.__states.push(state);
    if (notice && window.__notices.at(-1) !== notice) window.__notices.push(notice);
  }, 50);
}
async function run(engine, root) {
  const report = { engine, environment: 'EMULATION, not physical devices', tts: 'MOCKED TTS',
    latencyLabel: 'mock pipeline latency; synthetic microphone, fake STT/Gateway, MOCKED TTS',
    checks: [], consoleErrors: [], pageErrors: [], screenshots: [] };
  let browser, server, child, childError, origin, main, mainContext;
  const dir = path.join(root, engine), life = path.join(dir, 'life'), requests = [], contexts = new Set();
  const control = path.join(dir, 'rate-limit'), phrases = path.join(dir, 'phrases.json');
  const gateway = { nextLong: false, nextDelay: 650 };
  const firstLong = 'Это длинное синтетическое предложение для проверки прерывания озвучки настоящим входящим аудиосигналом.';
  async function check(name, fn) {
    const start = Date.now();
    try { const details = await fn(); report.checks.push({ name, status: 'pass', details: details ?? {}, ms: Date.now() - start }); }
    catch (e) { report.checks.push({ name, status: 'fail', details: clean(e.stack || e), ms: Date.now() - start }); process.exitCode = 1; }
    console.log(`${engine}: ${name}: ${report.checks.at(-1).status}`);
  }
  const skip = name => report.checks.push({ name, status: 'not-run', details: 'WebKit fake microphone unsupported; text/UI checks still run.' });
  async function api(route, body) {
    const response = await fetch(origin + route, { signal: AbortSignal.timeout(5000), headers: { Origin: origin, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }) });
    assert.equal(response.status, 200, route); return response.json();
  }
  async function setPhrases(values) { await fs.writeFile(phrases + '.new', JSON.stringify(values)); await fs.rename(phrases + '.new', phrases); }
  const runs = async () => (await api('/api/agent')).runs;
  const input = r => r.input.replace(/^\[Голосовой запрос[^\n]*\]\s*/, '');
  async function completed(p, text) {
    const run = await until(async () => (await runs()).find(r => input(r) === text && r.status === 'completed'), 'No completed run: ' + text, 40000);
    await until(async () => (await p.locator('#voice-history article').allTextContents()).some(t => t.includes(text) && t.includes(run.output)), 'Answer absent from history');
    assert.equal(run.model, 'fake-model'); return run;
  }
  async function boot(p) {
    await p.goto(origin, { waitUntil: 'domcontentloaded' });
    await p.waitForFunction(() => window.__svodkaBooted === true, null, { timeout: 14000 });
    assert.equal(await p.locator('#boot-error').isVisible(), false);
  }
  async function panel(p) {
    if (!await p.locator('#voice').isVisible()) await p.locator('#voice-btn').click();
    if (await p.locator('#voice').getAttribute('data-collapsed') === 'true') await p.locator('#voice-collapse').click();
  }
  async function send(p, text) { await panel(p); await p.locator('#voice-text').fill(text); await p.locator('#voice-send').click(); }
  const state = (p, value, timeout = 15000) => p.waitForFunction(s => document.querySelector('#voice').dataset.state === s, value, { timeout });
  async function makePage(options = {}, denied = false) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, ...options }); contexts.add(ctx);
    if (engine === 'chromium' && !denied) await ctx.grantPermissions(['microphone'], { origin });
    await ctx.addInitScript(browserMocks, { denied });
    await ctx.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const p = await ctx.newPage(); p.setDefaultTimeout(10000);
    p.on('console', m => { if (m.type() === 'error') report.consoleErrors.push(clean(m.text())); });
    p.on('pageerror', e => report.pageErrors.push(clean(e.message)));
    return { p, ctx };
  }
  async function fresh(fn, options = {}, denied = false) {
    // A failed scenario must not leave a long-answer switch or active run in the next scenario.
    gateway.nextLong = false; gateway.nextDelay = 650;
    await until(async () => (await runs()).every(r => r.status !== 'running'), 'Previous run still active', 30000);
    const { p, ctx } = await makePage(options, denied);
    try { await boot(p); await panel(p); return await fn(p, ctx); } finally { await ctx.close(); contexts.delete(ctx); }
  }
  async function shot(p, label) {
    const file = `browser-${engine}-${label}.png`;
    await p.screenshot({ path: path.join(evidence, file), animations: 'disabled' }); report.screenshots.push(file);
  }
  try {
    const pw = require('/home/jarvis/.npm-global/lib/node_modules/openclaw/node_modules/playwright-core');
    await fs.mkdir(life, { recursive: true }); await fs.writeFile(path.join(dir, 'speech.wav'), wav());
    await fs.writeFile(path.join(dir, 'agent-token'), 'synthetic-voice-test-token', { mode: 0o600 });
    await setPhrases(Array.from({ length: 16 }, (_, i) => `Синтетическая реплика номер ${i + 1}`));
    const stt = path.join(dir, 'fake-stt.py');
    await fs.writeFile(stt, `#!/usr/bin/python3
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
time.sleep(0.35)
if (root / 'rate-limit').exists():
    with (root / 'stt.jsonl').open('a') as f: f.write(json.dumps({'error':'rate-limit'})+'\\n')
    print('fake provider rate-limit: 429, try again', file=sys.stderr)
    sys.exit(1)
counter = root / 'stt-index'
i = int(counter.read_text()) if counter.exists() else 0
phrases = json.loads((root / 'phrases.json').read_text())
text = phrases[i % len(phrases)]
counter.write_text(str(i + 1))
with (root / 'stt.jsonl').open('a') as f: f.write(json.dumps({'text':text})+'\\n')
print(json.dumps({'ok':True,'outputs':[{'text':text}],'provider':'fake','model':'fake'}))
`, { mode: 0o700 });
    server = http.createServer(async (req, res) => {
      if (req.method !== 'POST' || req.url !== '/v1/responses') { res.writeHead(404).end(); return; }
      try {
        let body = ''; for await (const chunk of req) body += chunk;
        const record = { body: JSON.parse(body), long: gateway.nextLong }; requests.push(record);
        const wait = gateway.nextDelay; gateway.nextLong = false; gateway.nextDelay = 650;
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
        const emit = (type, data) => { if (!res.destroyed) res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`); };
        emit('response.created', { response: { model: 'fake-model' } }); await delay(wait);
        const chunks = record.long ? [firstLong, ...Array.from({ length: 7 }, (_, i) => `Продолжение длинного ответа номер ${i + 1}.`)]
          : ['Ответ готов.', 'Проверка пройдена.', 'Можно продолжать.'];
        record.answer = chunks.map(chunk => chunk + ' ').join('');
        for (const chunk of chunks) { if (res.destroyed) break; emit('response.output_text.delta', { delta: chunk + ' ' }); await delay(record.long ? 900 : 150); }
        emit('response.completed', { response: { model: 'fake-model', status: 'completed' } }); res.end();
      } catch { if (!res.destroyed) res.destroy(); }
    });
    const gwPort = await listen(server), reservation = http.createServer(), port = await listen(reservation);
    await closeServer(reservation); origin = `http://127.0.0.1:${port}`;
    const env = { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: dir, TMPDIR: dir, XDG_CONFIG_HOME: dir, XDG_CACHE_HOME: dir };
    // -B also prevents imported dashboard siblings from creating bytecode in the repository.
    child = spawn('/usr/bin/python3', ['-I', '-B', path.join(dashboard, 'server.py')], { cwd: dir, detached: true,
      env: { ...env, LIFE_DIR: life, DASHBOARD_HOST: '127.0.0.1', DASHBOARD_PORT: String(port),
        DASHBOARD_TOKEN_FILE: path.join(dir, 'absent-dashboard-token'), SVODKA_OPENCLAW_BIN: stt,
        SVODKA_VOICE_ENABLED: '1', SVODKA_AGENT_TRUSTED_PROXY: '1', SVODKA_AGENT_ENABLED: '1',
        SVODKA_AGENT_URL: `http://127.0.0.1:${gwPort}/v1/responses`, SVODKA_AGENT_ID: 'svodka-test',
        SVODKA_AGENT_USER: 'svodka-qa', SVODKA_AGENT_TOKEN_FILE: path.join(dir, 'agent-token') }, stdio: ['ignore', 'pipe', 'pipe'] });
    let serverLog = ''; child.stdout.on('data', b => { serverLog = (serverLog + b).slice(-6000); });
    child.stderr.on('data', b => { serverLog = (serverLog + b).slice(-6000); }); child.on('error', e => { childError = e; });
    await until(async () => {
      if (childError || child.exitCode !== null) throw new Error(clean(childError || serverLog));
      try { return (await api('/api/voice')).stt; } catch { return false; }
    }, 'Disposable server did not start', 15000);
    browser = await pw[engine].launch({ headless: true, env, ...(engine === 'chromium' ? {
      executablePath: '/opt/google/chrome/chrome', args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
        `--use-file-for-fake-audio-capture=${path.join(dir, 'speech.wav')}`] } : {}) });
    report.browserVersion = browser.version();
    await check('boot', async () => { ({ p: main, ctx: mainContext } = await makePage()); await boot(main); assert.equal(report.pageErrors.length, 0); });
    await check('boot-hang', async () => {
      const { p, ctx } = await makePage(); const start = Date.now();
      try {
        await p.route('**/api/data', () => {}); // Intentionally leave the request pending.
        await p.goto(origin, { waitUntil: 'domcontentloaded' });
        await p.locator('#boot-error').waitFor({ state: 'visible', timeout: Math.max(1, 14000 - (Date.now() - start)) });
        assert.match(await p.locator('#boot-error').innerText(), /этап/);
        assert(await p.locator('#boot-error').getByRole('button', { name: 'Повторить' }).isVisible());
        const visibleMs = Date.now() - start; assert(visibleMs <= 14000);
        const log = await until(async () => fs.readFile(path.join(life, 'dashboard-agent/client-log.jsonl'), 'utf8').catch(() => ''), 'No client-log POST persisted');
        assert(!log.includes('?k=')); await shot(p, 'boot-hang'); return { visibleMs, clientLog: JSON.parse(log.trim().split('\n').at(-1)) };
      } finally { await ctx.close(); contexts.delete(ctx); await main?.bringToFront(); }
    });
    if (engine === 'chromium') {
      await check('continuous', async () => {
        await panel(main); await main.locator('#voice-start').click();
        await until(async () => (await runs()).filter(r => r.status === 'completed').length >= 3, 'Three autonomous turns did not complete', 90000);
        const completedRuns = (await runs()).filter(r => r.status === 'completed');
        for (const r of completedRuns.slice(0, 3)) await completed(main, input(r));
        assert(await main.locator('#voice-history .voice-input').count() >= 3);
        assert((await main.locator('#voice-history .voice-output').allTextContents()).filter(t => t.trim()).length >= 3);
        assert(requests.length >= 3); for (const r of requests) assert.match(r.body.input, /^\[Голосовой запрос из Сводки\. Контекст: вкладка Личное\]\n/);
        const states = await main.evaluate(() => window.__states);
        for (const s of ['listening', 'transcribing', 'thinking', 'speaking']) assert(states.includes(s), `Missing state ${s}`);
        assert(await main.locator('#voice-mic-live').isVisible()); await shot(main, 'continuous'); return { completed: completedRuns.length, states };
      });
      await check('latency', async () => {
        const metrics = await until(async () => { const m = await api('/api/voice/metrics'); return m.stages.t_first_audio.n >= 3 && m; }, 't_first_audio n < 3', 30000);
        report.latency = metrics; return { label: report.latencyLabel, ...metrics };
      });
      await check('barge-in', async () => {
        const cancelled = await main.evaluate(() => window.__spokenCancelled);
        await setPhrases(['Синтетическое начало длинного ответа']); gateway.nextLong = true;
        await until(() => requests.some(r => r.long), 'Long answer not requested', 30000);
        await until(() => main.evaluate(text => window.__spoken.includes(text) && document.querySelector('#voice').dataset.state === 'speaking', firstLong), 'Long answer never speaking', 15000);
        await setPhrases([firstLong]);
        await until(() => main.evaluate(text => window.__cancelledTexts.includes(text), firstLong), 'Microphone burst did not cancel the long TTS sentence', 10000);
        assert(await main.evaluate(n => window.__spokenCancelled > n, cancelled));
        await until(() => main.evaluate(() => window.__notices.some(t => t.includes('Эхо озвучки пропущено'))), 'Injected spoken echo was not rejected', 15000);
        assert(!requests.some(r => input(r.body) === firstLong), 'Echo reached Gateway');
        await setPhrases(['Синтетическая новая реплика после перебивания']);
        await completed(main, 'Синтетическая новая реплика после перебивания');
        return { cancelled: await main.evaluate(() => window.__spokenCancelled), injectedEchoRejected: true };
      });
      await check('stop', async () => {
        await main.locator('#voice-stop').click(); await state(main, 'idle');
        assert(!await main.locator('#voice-mic-live').isVisible());
        assert(await main.evaluate(() => window.__tracks.length > 0 && window.__tracks.every(t => t.readyState === 'ended')));
        const n = requests.length; await delay(5000); assert.equal(requests.length, n); return { requestsStableForMs: 5000, tracksEnded: true };
      });
    } else for (const name of ['continuous', 'barge-in', 'stop', 'latency', 'STT rate limit', 'permission denied']) skip(name);
    await mainContext?.close(); contexts.delete(mainContext);
    await check('tabs', () => fresh(async p => {
      const text = 'Синтетическая проверка вкладок'; gateway.nextDelay = 7000; await send(p, text); await state(p, 'thinking');
      await until(() => requests.some(r => input(r.body) === text), 'Turn not accepted');
      const before = await p.locator('#voice-history .voice-input').allTextContents();
      for (const tab of ['life', 'work', 'graph', 'ai', 'agents']) {
        await p.locator('#tab-' + tab).click(); await p.locator('#page-' + tab).waitFor({ state: 'visible' });
        assert(await p.locator('#voice').isVisible()); assert.deepEqual(await p.locator('#voice-history .voice-input').allTextContents(), before);
      }
      await p.locator('#office-fit').click(); await p.locator('#tab-life').click(); const run = await completed(p, text);
      assert.deepEqual(await p.locator('#voice-history .voice-input').allTextContents(), before);
      await p.locator('#tab-agents').click();
      await until(async () => (await p.locator('#office-list').innerText()).includes(`Сводка #${run.id}`), 'Run absent from Agents/Office');
      const recent = (await runs()).sort((a, b) => b.updated - a.updated).slice(0, 6);
      await until(async () => { const labels = await p.locator('#office-list .office-task-meta').allTextContents();
        return recent.every(r => labels.some(t => t.startsWith(`Сводка #${r.id} ·`))); }, 'Office missing recent shared runs');
      const history = await p.locator('#voice-history .voice-input').allTextContents();
      assert.deepEqual(await p.locator('.agent-history .msg.user').allTextContents(), history);
      return { tabs: ['Личное', 'Работа', 'Память', 'ИИ', 'Агенты / Офис'], note: 'Офис is the Агенты page; it displays up to six recent completed runs.', run: run.id, officeRuns: recent.map(r => r.id) };
    }));
    await check('dedupe', () => fresh(async p => {
      const text = 'Синтетическая проверка дедупликации'; let attempts = 0, acceptedStatus, routeError; const bodies = [];
      await p.route('**/api/agent/send', async route => {
        try {
          bodies.push(route.request().postDataJSON());
          if (++attempts === 1) { const response = await route.fetch(); acceptedStatus = response.status(); await response.dispose(); await route.abort('failed'); }
          else await route.continue();
        } catch (e) { routeError = clean(e); await route.abort('failed').catch(() => {}); }
      });
      await send(p, text); await until(() => attempts > 0, 'Send not intercepted');
      await until(async () => { if (await p.locator('#voice-retry').isVisible()) await p.locator('#voice-retry').click(); return attempts >= 2; }, 'No retry', 20000);
      await completed(p, text); await delay(3500);
      assert.equal(routeError, undefined); assert.equal(acceptedStatus, 202, 'The first send must have created a run on the server');
      assert(bodies.length >= 2); assert(bodies.every(b => b.request === bodies[0].request && b.text === bodies[0].text));
      assert.equal(requests.filter(r => input(r.body) === text).length, 1);
      assert.equal((await runs()).filter(r => input(r) === text).length, 1);
      assert.equal((await p.locator('#voice-history .voice-input').allTextContents()).filter(t => t === text).length, 1);
      return { attempts, gatewayRequests: 1, historyRows: 1, sameRequestId: true };
    }));
    await check('offline', () => fresh(async (p, ctx) => {
      try { await ctx.setOffline(true); await state(p, 'offline'); assert(await p.locator('#voice-status').isVisible());
        assert.match(await p.locator('#voice-status').innerText(), /Нет связи/); }
      finally { await ctx.setOffline(false); }
      await state(p, 'idle'); await send(p, 'Синтетическая реплика после сети'); await completed(p, 'Синтетическая реплика после сети');
    }));
    if (engine === 'chromium') {
      await check('STT rate limit', () => fresh(async p => {
        await fs.writeFile(control, 'rate-limit');
        try {
          await p.locator('#voice-start').click();
          await until(async () => /rate-limit/.test(await p.locator('#voice-notice').innerText()), 'STT error notice absent', 20000);
          assert(await p.locator('#voice-notice').isVisible()); await state(p, 'listening');
          await setPhrases(['Синтетическое восстановление распознавания']); await fs.rm(control);
          await completed(p, 'Синтетическое восстановление распознавания'); assert(await p.locator('#voice-mic-live').isVisible());
        } finally { await fs.rm(control, { force: true }); }
      }));
      await check('permission denied', () => fresh(async p => {
        await p.locator('#voice-start').click(); await state(p, 'error');
        assert.match(await p.locator('#voice-notice').innerText(), /Доступ к микрофону запрещён/);
        assert(await p.locator('#voice-notice').isVisible());
        await send(p, 'Синтетический текст без микрофона'); await completed(p, 'Синтетический текст без микрофона');
        return { permission: 'getUserMedia rejects NotAllowedError; no context microphone grant' };
      }, {}, true));
    }
    await check('text turn + forms', () => fresh(async p => {
      await send(p, 'Синтетическая проверка формы'); await completed(p, 'Синтетическая проверка формы');
      await p.locator('#tab-life').click(); await p.locator('#qa-open').click();
      assert(await p.locator('#qa').evaluate(d => d.matches(':modal') && d.contains(document.elementFromPoint(d.getBoundingClientRect().x + d.clientWidth / 2, d.getBoundingClientRect().y + d.clientHeight / 2))));
      assert(await p.locator('#voice').isVisible()); await p.locator('#qa [data-qa="tasks"]').click();
      const form = p.locator('#qa [data-qa-form="tasks"]'), title = 'Синтетическая задача из формы';
      await form.locator('[name="title"]').fill(title); await form.locator('[type="submit"]').click();
      await until(async () => (await api('/api/data')).tasks.tasks.some(t => t.title === title), 'Form task not persisted');
      // A successful save closes the dialog itself; close it only if it is still open.
      await p.locator('#qa-close').click({ timeout: 2000 }).catch(() => {}); await until(async () => !(await p.locator('#qa').isVisible()), 'Form dialog stayed open');
      assert(await p.locator('#voice').isVisible()); return { task: title, modalAbovePanel: true };
    }));
    await check('memory write/read — data diff reporting', () => fresh(async p => {
      const text = 'запиши задачу: синтетическая проверка изменений', title = 'Синтетическая задача для сравнения';
      gateway.nextDelay = 3500; await send(p, text);
      await until(() => requests.some(r => input(r.body) === text), 'Run did not start');
      await api('/api/tasks/add', { list: 'tasks', title }); await completed(p, text);
      await until(async () => /Дела: \+1/.test(await p.locator('#voice-diff').innerText()), 'Panel did not report task diff');
      assert((await api('/api/data')).tasks.tasks.some(t => t.title === title));
      return { label: 'Test POST writes task during run; proves persisted read-back and panel diff reporting, NOT agent memory-writing ability.' };
    }));
    // Chromium cannot take WebKit's device profiles' userAgent semantics, so it gets viewport/touch only.
    const mobile = (d, webkit) => webkit ? d : { viewport: d.viewport, deviceScaleFactor: d.deviceScaleFactor, isMobile: d.isMobile, hasTouch: d.hasTouch };
    const devices = [['desktop', {}], ['ipad', mobile(pw.devices['iPad Pro 11'], engine === 'webkit')], ['iphone', mobile(pw.devices['iPhone 15'], engine === 'webkit')]];
    for (const [label, options] of devices) await check('devices: ' + label, () => fresh(async p => {
      assert(options, 'Missing Playwright device descriptor'); await send(p, `Синтетическая проверка устройства ${engine} ${label}`);
      await completed(p, `Синтетическая проверка устройства ${engine} ${label}`);
      const geometry = await p.locator('#voice').evaluate(el => { const r = el.getBoundingClientRect(); return {
        x: r.x, y: r.y, right: r.right, bottom: r.bottom, width: innerWidth, height: innerHeight, overflow: document.documentElement.scrollWidth > innerWidth + 1 }; });
      assert(geometry.x >= -1 && geometry.y >= -1 && geometry.right <= geometry.width + 1 && geometry.bottom <= geometry.height + 1);
      assert(!geometry.overflow); assert(await p.locator('#voice').isVisible()); await shot(p, label);
      return { label: 'EMULATION, not physical devices', boot: true, textTurn: true, ...geometry };
    }, options));
    await check('reduced motion', () => fresh(async p => {
      await p.emulateMedia({ reducedMotion: 'reduce' }); await p.locator('#voice-close').click(); await panel(p); await delay(100);
      const animations = await p.locator('#voice').evaluate(el => el.getAnimations({ subtree: true }).filter(a => ['running', 'pending'].includes(a.playState)).length);
      assert.equal(animations, 0); return { activeAnimations: animations };
    }));
    await check('no page errors', async () => { assert.deepEqual(report.pageErrors, []); return { consoleErrorsRecorded: report.consoleErrors.length, note: 'Fault injection intentionally produces console network errors.' }; });
  } catch (e) { await check('setup / suite', async () => { throw e; }); }
  finally {
    await check('cleanup', async () => {
      const errors = [];
      for (const ctx of contexts) await ctx.close().catch(e => errors.push(clean(e)));
      if (browser) await browser.close().catch(e => errors.push(clean(e)));
      if (child?.pid) {
        try { process.kill(-child.pid, 'SIGTERM'); } catch (e) { if (e.code !== 'ESRCH') errors.push(clean(e)); }
        await Promise.race([new Promise(resolve => child.exitCode !== null || child.signalCode ? resolve() : child.once('exit', resolve)), delay(2000)]);
        try { process.kill(-child.pid, 'SIGKILL'); } catch (e) { if (e.code !== 'ESRCH') errors.push(clean(e)); }
      }
      if (server) await closeServer(server).catch(e => errors.push(clean(e)));
      report.gateway = { count: requests.length, requests };
      report.stt = await fs.readFile(path.join(dir, 'stt.jsonl'), 'utf8').then(s => s.trim().split('\n').filter(Boolean).map(JSON.parse)).catch(() => []);
      await fs.rm(dir, { recursive: true, force: true }); assert.deepEqual(errors, []);
    });
    report.ok = report.checks.every(c => c.status !== 'fail');
    await fs.writeFile(path.join(evidence, `browser-${engine}.json`), clean(JSON.stringify(report, null, 2)) + '\n');
  }
}
(async () => {
  await fs.mkdir(evidence, { recursive: true }); const root = await fs.mkdtemp(path.join(os.tmpdir(), 'svodka-browser-voice-'));
  // Playwright creates profiles/artifacts using the parent process's os.tmpdir(), not launch.env.
  const oldTmp = process.env.TMPDIR; process.env.TMPDIR = root;
  try { for (const engine of engines) await run(engine, root); }
  finally { await fs.rm(root, { recursive: true, force: true });
    if (oldTmp === undefined) delete process.env.TMPDIR; else process.env.TMPDIR = oldTmp; }
})().catch(e => { console.error(clean(e.stack || e)); process.exitCode = 1; });
