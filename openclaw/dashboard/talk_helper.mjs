// One live Jarvis voice call: keeps an OpenClaw Gateway connection open for the call,
// because the Gateway drops a realtime session when the connection that created it closes.
// Protocol on stdin/stdout, one JSON object per line:
//   out {ready, model, voice}            after talk.client.create
//   in  {offer: "<sdp>"}  -> out {answer: "<sdp>"} or {error}
//   in  {close: true} or stdin end -> talk.client.close, exit
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

const say = (obj) => process.stdout.write(JSON.stringify(obj) + '\n');
const fail = (msg) => { say({ error: msg }); process.exit(1); };
const gateway = new URL(process.env.GATEWAY_URL || 'http://127.0.0.1:18789/');
if (!['127.0.0.1', 'localhost', '[::1]'].includes(gateway.hostname)) fail('Шлюз должен быть на этом сервере');
const base = `${gateway.protocol}//${gateway.host}`;
let token;
try { token = readFileSync(process.env.TOKEN_FILE, 'utf8').trim(); } catch { fail('Нет токена шлюза'); }
const { GatewayClient } = await import(process.env.OPENCLAW_SDK);

let session = null, closing = false;
async function close(code = 0) {
  if (closing) return;
  closing = true;
  if (session) await client.request('talk.client.close', { sessionKey: session.key, voiceSessionId: session.voiceSessionId }).catch(() => {});
  client.stop();
  process.exit(code);
}
const client = new GatewayClient({
  url: base.replace(/^http/, 'ws'), token, clientName: 'cli', mode: 'cli', role: 'operator',
  onConnectError: () => fail('Не удалось подключиться к Джарвису'),
  onClose: () => { if (!closing) process.exit(1); },
  onHelloOk: async () => {
    try {
      const key = process.env.SESSION_KEY || 'main';
      const s = await client.request('talk.client.create', { mode: 'realtime', transport: 'webrtc', brain: 'agent-consult', sessionKey: key, capabilities: ['gateway-control-v1'] });
      session = { ...s, key };
      say({ ready: true, model: s.model || null, voice: s.voice || null });
    } catch (e) { fail('Джарвис не открыл голосовую сессию: ' + String(e.message || e).slice(0, 160)); }
  },
});
client.start();

createInterface({ input: process.stdin }).on('line', async (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.close) return close();
  if (typeof msg.offer === 'string' && session) {
    try {
      const r = await fetch(base + session.offerUrl, { method: 'POST', body: msg.offer,
        headers: { Authorization: 'Bearer ' + session.clientSecret, 'Content-Type': 'application/sdp' } });
      const text = await r.text();
      say(r.ok ? { answer: text } : { error: 'Голос не подключился (' + r.status + '): ' + text.slice(0, 160) });
    } catch { say({ error: 'Голосовой сервер не ответил' }); }
  }
}).on('close', () => close());
setTimeout(() => close(), 30 * 60 * 1000).unref();
