"""What Jarvis is doing right now: the tool steps of one OpenClaw session, read from its transcript.

The Responses stream that the chats use carries only the final text, so while Jarvis works the panel
would show nothing. OpenClaw writes every tool call to the agent's database as it happens; this reads
those rows, read-only, and turns each into one plain line ("Читает файл …", "Готово", "Ошибка"). File
contents, command output and tool results never leave: only a short title, a status and the time.
"""
import json
import re
import sqlite3
from pathlib import Path

LIMIT = 80
TITLE_LIMIT = 140
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
            for part in message.get('content') or []:
                if isinstance(part, dict) and part.get('type') == 'toolCall':
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
