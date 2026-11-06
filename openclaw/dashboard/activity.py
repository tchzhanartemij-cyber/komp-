"""What Jarvis is doing right now: the tool steps of one OpenClaw session, read from its transcript.

The Responses stream that the chats use carries only the final text, so while Jarvis works the panel
would show nothing. OpenClaw writes every tool call to the agent's database as it happens; this reads
those rows, read-only, and turns each into one plain line ("Читает файл …", "Готово", "Ошибка"). File
contents and tool results stay out of the list: it carries short titles, Jarvis's own progress notes
and thinking headlines. What a step ran and returned is fetched one step at a time, when the owner
opens it (`detail`), redacted and capped.
"""
import json
import re
import sqlite3
from pathlib import Path

LIMIT = 80
TITLE_LIMIT = 140
NOTE_LIMIT = 400
DETAIL_LIMIT = 4000
NAMES = {
    'exec': 'Выполняет действие',
    'wait': 'Ждёт, пока закончится работа',
    'sessions_yield': 'Передаёт работу',
    'sessions_send': 'Пишет в другую сессию',
    'view_image': 'Смотрит картинку',
    'session_status': 'Проверяет состояние сессии',
    'message': 'Отправляет сообщение',
    'openclaw': 'Команда OpenClaw',
    'memory_search': 'Ищет в памяти',
    'memory_get': 'Читает память',
    'web_search': 'Ищет в интернете',
    'web_fetch': 'Открывает страницу',
}


def database(agent):
    return Path.home() / '.openclaw/agents' / agent / 'agent/openclaw-agent.sqlite'


def _title(name, args, secret_re):
    if isinstance(args, dict) and isinstance(args.get('title'), str) and args['title'].strip():
        text = args['title'].strip()
    else:
        text = NAMES.get(name, 'Инструмент ' + str(name or ''))
        if isinstance(args, dict):
            detail = next((args[k] for k in ('path', 'query', 'url', 'q') if isinstance(args.get(k), str)), '')
            if detail:
                text += ': ' + (Path(detail).name if 'path' in args else detail)
    text = secret_re.sub('***', ' '.join(text.split()))
    return text[:TITLE_LIMIT] + ('…' if len(text) > TITLE_LIMIT else '')


def _short(text, secret_re, limit):
    text = secret_re.sub('***', ' '.join(str(text).split()))
    return text[:limit] + ('…' if len(text) > limit else '')


def _failed(message):
    if message.get('isError'):
        return True
    text = ''.join(p.get('text', '') for p in message.get('content') or [] if isinstance(p, dict))
    return bool(re.match(r'\s*\{\s*"status"\s*:\s*"(failed|error)"', text))


def steps(agent, session_key, after, secret_re):
    """Tool steps of the session after transcript position `after`. Returns (steps, last position)."""
    path = database(agent)
    if not path.is_file():
        return [], after
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)
    try:
        row = db.execute('select current_session_id from session_nodes where session_key = ?', (session_key,)).fetchone()
        if not row or not row[0]:
            return [], after
        rows = db.execute('select seq, event_json, created_at from transcript_events where session_id = ? and seq > ? '
                          'order by seq limit 400', (row[0], after)).fetchall()
    finally:
        db.close()
    out, calls, last = [], {}, after
    for seq, raw, created in rows:
        last = max(last, seq)
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        message = event.get('message') if isinstance(event.get('message'), dict) else {}
        role = message.get('role')
        if role == 'assistant':
            for n, part in enumerate(message.get('content') or []):
                if not isinstance(part, dict):
                    continue
                # Headlines of the model's thinking and the short notes it writes between tools, as in Claude Code.
                if part.get('type') == 'thinking' and isinstance(part.get('thinking'), str) and part['thinking'].strip():
                    head = part['thinking'].strip().splitlines()[0].strip('*# ').strip()
                    if head:
                        out.append(dict(id=f't{seq}.{n}', at=created, title=_short(head, secret_re, TITLE_LIMIT), state='think'))
                elif part.get('type') == 'text' and isinstance(part.get('text'), str) and part['text'].strip() and \
                        any(isinstance(x, dict) and x.get('type') == 'toolCall' for x in message.get('content') or []):
                    out.append(dict(id=f'n{seq}.{n}', at=created, title=_short(part['text'], secret_re, NOTE_LIMIT), state='note'))
                elif part.get('type') == 'toolCall':
                    step = dict(id=str(part.get('id') or seq), at=created, title=_title(part.get('name'), part.get('arguments'), secret_re), state='running')
                    calls[step['id']] = step
                    out.append(step)
        elif role == 'toolResult':
            step = calls.get(str(message.get('toolCallId')))
            if step is None:
                step = dict(id=str(message.get('toolCallId') or seq), at=created, title=NAMES.get(message.get('toolName'), 'Инструмент'), state='running')
                out.append(step)
            step['state'] = 'failed' if _failed(message) else 'done'
            step['done_at'] = created
        elif role == 'user':
            out.append(dict(id='u' + str(seq), at=created, title='Новое сообщение', state='input'))
    return out[-LIMIT:], last


def position(agent, session_key):
    """Current end of the session's transcript, so a new turn reports only its own steps."""
    path = database(agent)
    if not path.is_file():
        return 0
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)
    try:
        row = db.execute('select current_session_id from session_nodes where session_key = ?', (session_key,)).fetchone()
        if not row or not row[0]:
            return 0
        found = db.execute('select max(seq) from transcript_events where session_id = ?', (row[0],)).fetchone()
        return found[0] if found and found[0] is not None else 0
    finally:
        db.close()


def _pretty(value, secret_re):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    text = secret_re.sub('***', text)
    return text[:DETAIL_LIMIT] + ('\n…' if len(text) > DETAIL_LIMIT else '')


def _readable(result):
    """The part of a tool result a person reads: command output first, the envelope only as a fallback."""
    if not isinstance(result, dict):
        return result
    value = result.get('value')
    if isinstance(value, dict):
        text = next((value[k] for k in ('aggregated', 'stdout', 'tail', 'text', 'content') if isinstance(value.get(k), str) and value[k].strip()), None)
        if text is not None:
            code = value.get('exitCode')
            return text + (f'\n\n(код выхода {code})' if isinstance(code, int) and code else '')
    if isinstance(value, str) and value.strip():
        return value
    parts = [o.get('text') for o in result.get('output') or [] if isinstance(o, dict) and isinstance(o.get('text'), str)]
    if parts:
        return '\n'.join(parts)
    if result.get('error'):
        return str(result['error'])
    return value if value is not None else {k: v for k, v in result.items() if k not in ('telemetry', 'replaySafe')}


def detail(agent, session_key, step_id, secret_re):
    """What one tool step ran and what it got back. None when the step is no longer in the transcript."""
    path = database(agent)
    if not path.is_file() or not step_id or len(step_id) > 200:
        return None
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)
    try:
        row = db.execute('select current_session_id from session_nodes where session_key = ?', (session_key,)).fetchone()
        if not row or not row[0]:
            return None
        like = '%' + step_id.replace('%', '').replace('_', '\\_') + '%'
        rows = db.execute("select event_json from transcript_events where session_id = ? and event_json like ? escape '\\' "
                          'order by seq desc limit 4', (row[0], like)).fetchall()
    finally:
        db.close()
    found = {}
    for (raw,) in rows:
        try:
            message = json.loads(raw).get('message') or {}
        except (ValueError, AttributeError):
            continue
        if message.get('role') == 'assistant':
            for part in message.get('content') or []:
                if isinstance(part, dict) and part.get('type') == 'toolCall' and str(part.get('id')) == step_id:
                    args = part.get('arguments') if isinstance(part.get('arguments'), dict) else {}
                    body = args.get('code') if isinstance(args.get('code'), str) else {k: v for k, v in args.items() if k != 'title'}
                    found['tool'] = part.get('name')
                    found['input'] = _pretty(body, secret_re)
        elif message.get('role') == 'toolResult' and str(message.get('toolCallId')) == step_id:
            text = ''.join(p.get('text', '') for p in message.get('content') or [] if isinstance(p, dict))
            try:
                value = _readable(json.loads(text))
            except ValueError:
                value = text
            found['output'] = _pretty(value, secret_re)
            found['failed'] = _failed(message)
    return found or None
