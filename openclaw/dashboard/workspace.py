"""Restricted, server-side canonical workspace gateway adapter."""
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

SECRET_RE = re.compile(r'(sk|gsk|xai|key)[-_][\w-]{8,}|Bearer \S+', re.I)
CANCEL = {'supported': False, 'reason': 'Отмена через поддерживаемый интерфейс панели недоступна: кнопка остановила бы только интерфейс, не выполнение'}


def timestamp(value, milliseconds=False):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return None
        return value / 1000 if milliseconds or abs(value) > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


class Workspace:
    def __init__(self, agent, secret_re=SECRET_RE):
        self.agent = agent
        self.secret_re = secret_re
        self.lock = threading.RLock()
        self.tail = None
        self.tail_time = 0

    def status(self):
        if os.environ.get('SVODKA_WORKSPACE_ENABLED') != '1':
            return dict(available=False, reason='Переписка отключена: требуется разрешение оператора SVODKA_WORKSPACE_ENABLED=1')
        try:
            if not self.agent.canonical():
                return dict(available=False, reason='Не задана SVODKA_CANONICAL_SESSION: требуется каноническая сессия оператора')
            self.agent.config()
        except Exception as e:
            # config/canonical only emit controlled operator diagnostics.
            missing = next((name for name in ('SVODKA_AGENT_ENABLED','SVODKA_AGENT_URL','SVODKA_AGENT_ID','SVODKA_AGENT_TOKEN_FILE') if not os.environ.get(name)), None)
            reason = ('Не задана '+missing+': ' if missing else '')+str(e)
            if os.environ.get('SVODKA_AGENT_ENABLED') != '1' and not missing:
                reason = 'Требуется разрешение оператора SVODKA_AGENT_ENABLED=1: '+str(e)
            return dict(available=False, reason=reason)
        return dict(available=True, reason='Настроено; доступность шлюза проверяется при чтении')

    def require(self):
        state = self.status()
        if not state['available']:
            raise ValueError(state['reason'])

    def gateway_invoke(self, tool, args):
        if tool not in ('sessions_history', 'sessions_list'):
            raise ValueError('Инструмент не разрешён')
        self.require()
        url, token, _, _ = self.agent.config()
        u = urlsplit(url)
        url = urlunsplit((u.scheme, u.netloc, '/tools/invoke', '', ''))
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        req = urllib.request.Request(url, json.dumps({'tool': tool, 'args': args, 'sessionKey': self.agent.canonical()}).encode(), {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(req, timeout=15) as response:
                raw = response.read(1024*1024+1)
                if len(raw) > 1024*1024:
                    raise ValueError()
                envelope = json.loads(raw)
                if envelope.get('ok') is not True:
                    raise ValueError()
                result = envelope['result']
                payload = result['details'] if isinstance(result.get('details'), dict) else json.loads(result['content'][0]['text'])
                if not isinstance(payload, dict):
                    raise ValueError()
                return payload
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            raise ValueError({401:'Шлюз отклонил авторизацию',403:'Политика шлюза запретила инструмент',404:'Инструмент недоступен по политике шлюза'}.get(code, 'Ошибка HTTP шлюза')) from None
        except Exception:
            raise ValueError('Шлюз недоступен или вернул некорректный ответ') from None

    def canonical_keys(self):
        key = self.agent.canonical()
        main = 'agent:'+self.agent.config()[2]+':main'
        # sessions_list reports the agent's main session under the display key "main".
        return {'main', main} if key in ('main', main) else {key}

    def sessions(self):
        payload = self.gateway_invoke('sessions_list', {'limit':50, 'includeLastMessage':False})
        keys = self.canonical_keys()
        out = []
        rows = payload.get('sessions', [])
        if not isinstance(rows,list):
            raise ValueError('Шлюз вернул некорректный список сессий')
        for row in rows:
            if not isinstance(row, dict):
                continue
            parent = row.get('parentSessionKey') or row.get('spawnedBy')
            if row.get('key') not in keys and parent not in keys:
                continue
            item = {k:row[k] for k in ('key','kind','channel','status','hasActiveRun','updatedAt') if isinstance(row.get(k), (str,int,float,bool))}
            for k in ('label','title','model'):
                if isinstance(row.get(k), str):
                    item[k] = self.secret_re.sub('***', row[k])[:120]
            if isinstance(parent, str):
                item['parent'] = parent
            out.append(item)
        return out

    def clean(self, text):
        tags = 'think|thinking|reasoning|relevant-memories|relevant_memories|tool_call|function_call|tool_calls|function_calls'
        text = re.sub(r'<('+tags+r')\b[^>]*>.*?(?:</\1\s*>|$)', '', text, flags=re.I|re.S)
        text = re.sub(r'<\|[^\r\n]*?\|>', '', text)
        return self.secret_re.sub('***', text)

    def history(self, offset=None, limit=30):
        self.require()
        offset = 0 if offset is None else int(offset)
        limit = int(limit)
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError('Неверная пагинация')
        with self.lock:
            cache_key = (self.agent.canonical(), limit)
            if offset == 0 and self.tail and self.tail[0] == cache_key and time.monotonic()-self.tail_time < 2:
                return self.tail[1]
            data = self.gateway_invoke('sessions_history', {'sessionKey':self.agent.canonical(), 'limit':limit, 'offset':offset, 'includeTools':False})
            channel = next((r.get('channel') for r in self.sessions() if r.get('key') in self.canonical_keys()), None) or 'unknown'
            messages, hidden, seen = [], 0, set()
            rows = data.get('messages', [])
            if not isinstance(rows,list):
                raise ValueError('Шлюз вернул некорректную историю')
            for msg in rows:
                if not isinstance(msg, dict) or msg.get('role') not in ('user','assistant'):
                    hidden += 1
                    continue
                role, content = msg['role'], msg.get('content', '')
                attachments = []
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') == 'text' and isinstance(block.get('text'), str):
                            texts.append(block['text'])
                        elif block.get('type') in ('image','audio','file','document'):
                            a = {'kind':block['type']}
                            if isinstance(block.get('name'), str):
                                a['name'] = self.clean(block['name'])[:120]
                            attachments.append(a)
                    content = '\n'.join(texts)
                text = self.clean(content if isinstance(content, str) else '')
                ts = timestamp(msg.get('timestamp'), milliseconds=True)
                meta = msg.get('__openclaw')
                mid = meta.get('id') if isinstance(meta, dict) else None
                synthetic = not isinstance(mid, str)
                if synthetic:
                    mid = hashlib.sha256((role+'|'+str(msg.get('timestamp'))+'|'+text).encode()).hexdigest()[:24]
                if mid in seen:
                    continue
                seen.add(mid)
                item = dict(id=mid, role=role, text=text[:8000], ts=ts, attachments=attachments, source='agent' if role=='assistant' else channel, synthetic_id=synthetic, truncated=len(text)>8000)
                origin = re.match(r'^\[Сводка:([A-Za-z0-9_-]{16,64})\]\s*', text) if role=='user' else None
                if origin:
                    item.update(source='dashboard', origin_request=origin[1], text=text[origin.end():][:8000])
                for field in ('edited','editedAt','deleted'):
                    if field in msg:
                        item['deleted' if field=='deleted' else 'edited'] = bool(msg[field])
                reply = msg.get('replyTo', msg.get('reply_to_id'))
                if isinstance(reply, (str,int)):
                    item['reply_to'] = reply
                messages.append(item)
            # Gateway order is the transcript order; rows without timestamps keep their position.
            result = dict(messages=messages, next_offset=data.get('nextOffset') if data.get('hasMore') else None, has_more=bool(data.get('hasMore')), gap=bool(data.get('truncated') or data.get('droppedMessages') or data.get('windowReset')), redacted=bool(data.get('contentRedacted')), hidden_count=hidden, fetched_at=time.time())
            if offset == 0:
                self.tail, self.tail_time = (cache_key, result), time.monotonic()
            return result

    def db(self):
        self.agent.root.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.agent.root/'workspace.sqlite3', timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, target TEXT, original TEXT, text TEXT, created REAL, request TEXT UNIQUE, run INTEGER, status TEXT CHECK(status IN ('draft','sent')))")
        return c

    def create_revision(self, target_id, original, text):
        self.require()
        if any(not isinstance(v,str) or not v.strip() or len(v)>20000 for v in (target_id, original, text)):
            raise ValueError('Неверные данные уточнения')
        with self.lock:
            c = self.db()
            try:
                with c:
                    rid = c.execute("INSERT INTO revisions(target,original,text,created,request,status) VALUES(?,?,?,?,?,'draft')", (target_id,original,text,time.time(),secrets.token_urlsafe(16))).lastrowid
                return dict(c.execute('SELECT * FROM revisions WHERE id=?',(rid,)).fetchone())
            finally:
                c.close()

    def send_revision(self, rid):
        self.require()
        with self.lock:
            c = self.db()
            try:
                row = c.execute('SELECT * FROM revisions WHERE id=?',(rid,)).fetchone()
                if row is None:
                    raise ValueError('Черновик не найден')
                if row['run'] is not None:
                    return dict(id=row['run'], duplicate=True)
                text = f"[Сводка:{row['request']}] Уточнение к ранее отправленному запросу (id {row['target']}). Исходный текст сохранён ниже без изменений. Уже выполненные действия не отменены; не повторяй их без явной необходимости.\nИсходный запрос:\n{row['original']}\nНовая редакция:\n{row['text']}"
                run = self.agent.submit(text, row['request'])
                with c:
                    c.execute("UPDATE revisions SET run=?,status='sent' WHERE id=?",(int(run),rid))
                return dict(id=int(run), duplicate=getattr(run,'duplicate',False))
            finally:
                c.close()

    def tasks(self, agent_snapshot, queue, sessions=None):
        out = []
        sources = [('dashboard', agent_snapshot.get('runs',[])), ('coder',queue.get('jobs',[])), ('session',self.sessions() if sessions is None else sessions)]
        for kind, rows in sources:
            for row in rows:
                raw = row.get('status') or ('running' if row.get('hasActiveRun') else 'unknown')
                if not isinstance(raw,str):
                    raw = 'unknown'
                status = {'waiting_limit':'queued','question':'waiting_approval','killed':'cancelled','done':'completed','merged':'completed','nochange':'completed','timeout':'failed','interrupted':'failed','incomplete':'failed'}.get(raw,raw)
                if status not in ('queued','running','waiting_approval','completed','failed','cancelled','unknown','stale'):
                    status = 'unknown'
                updated = timestamp(row.get('updated',row.get('updatedAt')))
                if status=='running' and updated is not None and time.time()-updated>900:
                    status='stale'
                item = dict(kind='subagent' if kind=='session' and row.get('parent') else kind, id=row.get('id',row.get('key')), status=status, raw_status=raw, cancel=dict(CANCEL))
                for key, value in [('title',row.get('title',row.get('label'))), ('model',row.get('model')), ('started',timestamp(row.get('started',row.get('created')))), ('updated',updated)]:
                    if value is not None and (key not in ('title','model') or isinstance(value,str)):
                        item[key] = self.clean(value)[:120] if isinstance(value,str) else value
                out.append(item)
        return out
