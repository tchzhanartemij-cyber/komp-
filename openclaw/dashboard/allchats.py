"""Read-only view of the owner's assistant chats: Claude Code, Codex and ChatGPT exports.

Everything comes from files on this server. Nothing here writes. The browser never sends a file
path: a chat is addressed by `source:id`, and that id is looked up in an index built from the
directories below. Every text leaves through the same secret redaction as the rest of the panel.
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path.home() / '.claude/projects'
CODEX_DIR = Path.home() / '.codex/sessions'
CHATGPT_DIR = Path.home() / '.openclaw/workspace/imports/chatgpt'
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,80}')
SYSTEM_RE = re.compile(r'<system-reminder>.*?</system-reminder>|<local-command-caveat>.*?</local-command-caveat>', re.S)
TEXT_LIMIT = 6000
MESSAGE_LIMIT = 400
LIST_LIMIT = 300

_lock = threading.Lock()
_files = {}      # path -> ((mtime, size), summary or None): Claude and Codex files parsed once per change
_index = {}      # "source:id" -> (kind, path)


def _title(text):
    line = ' '.join(text.split())
    return line[:90] + ('…' if len(line) > 90 else '') if line else 'Без названия'


def _iso(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')
    return str(ts or '')


def _claude_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')


def _claude_messages(path):
    out = []
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get('type') not in ('user', 'assistant') or o.get('isSidechain'):
                continue
            msg = o.get('message') or {}
            role = msg.get('role')
            if role not in ('user', 'assistant'):
                continue
            text = SYSTEM_RE.sub('', _claude_text(msg.get('content'))).strip()
            if not text or (role == 'user' and text.startswith('<') and text.endswith('>')):
                continue
            out.append(dict(role=role, text=text, time=_iso(o.get('timestamp')), cwd=o.get('cwd') or ''))
    return out


def _codex_messages(path):
    out, cwd = [], ''
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            payload = o.get('payload') or {}
            if o.get('type') == 'session_meta':
                cwd = payload.get('cwd') or cwd
                continue
            if o.get('type') != 'response_item' or payload.get('type') != 'message':
                continue
            role = payload.get('role')
            if role not in ('user', 'assistant'):
                continue
            text = '\n'.join(p.get('text', '') for p in payload.get('content') or [] if isinstance(p, dict))
            text = SYSTEM_RE.sub('', text).strip()
            # Codex injects its instructions and environment as user messages; they are not the owner's words.
            if not text or (role == 'user' and (text.startswith('<') or text.startswith('# AGENTS'))):
                continue
            out.append(dict(role=role, text=text, time=_iso(o.get('timestamp')), cwd=cwd))
    return out


def _chatgpt_conversations(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    for conv in data if isinstance(data, list) else []:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get('mapping') or {}
        node, chain = conv.get('current_node'), []
        while node in mapping and len(chain) < 5000:
            item = mapping[node]
            msg = item.get('message') or {}
            role = (msg.get('author') or {}).get('role')
            parts = (msg.get('content') or {}).get('parts') or []
            text = '\n'.join(p for p in parts if isinstance(p, str)).strip()
            if role in ('user', 'assistant') and text:
                chain.append(dict(role=role, text=text, time=_iso(msg.get('create_time')), cwd=''))
            node = item.get('parent')
        chain.reverse()
        yield dict(id=str(conv.get('id') or ''), title=conv.get('title') or '',
                   updated=_iso(conv.get('update_time') or conv.get('create_time')), messages=chain)


def _summary(source, chat_id, title, updated, messages, project, secret_re):
    first = next((m['text'] for m in messages if m['role'] == 'user'), '')
    return dict(id=f'{source}:{chat_id}', source=source, title=_title(secret_re.sub('***', title or first)),
                updated=updated, count=len(messages), project=project)


def _parsed(path, parse, summarize):
    """Parse a Claude or Codex file again only when its size or modification time changed."""
    st = path.stat()
    key = (st.st_mtime, st.st_size)
    hit = _files.get(path)
    if hit and hit[0] == key:
        return hit[1]
    value = summarize(path, parse(path))
    _files[path] = (key, value)
    return value


def _file_summary(source, path, messages, secret_re):
    first = next((m['text'] for m in messages if m['role'] == 'user'), '')
    # Service runs ("1" and an answer) are noise in the owner's list.
    if not messages or (len(messages) <= 2 and len(first.strip()) < 3):
        return None
    cwd = next((m['cwd'] for m in messages if m['cwd']), '')
    project = Path(cwd).name if cwd else ('Claude Code' if source == 'claude' else 'Codex')
    return _summary(source, path.stem, '', _iso(path.stat().st_mtime), messages, project, secret_re)


def _scan(secret_re):
    """Return the newest chats and the index that lets read_chat find them."""
    items, index = [], {}
    if CLAUDE_DIR.is_dir():
        for path in sorted(CLAUDE_DIR.glob('*/*.jsonl')):
            summary = _safe(lambda: _parsed(path, _claude_messages, lambda p, m: _file_summary('claude', p, m, secret_re)))
            if summary:
                index[summary['id']] = ('file', path)
                items.append(summary)
    if CODEX_DIR.is_dir():
        for path in sorted(CODEX_DIR.rglob('*.jsonl')):
            summary = _safe(lambda: _parsed(path, _codex_messages, lambda p, m: _file_summary('codex', p, m, secret_re)))
            if summary:
                index[summary['id']] = ('file', path)
                items.append(summary)
    if CHATGPT_DIR.is_dir():
        for path in sorted(CHATGPT_DIR.glob('conversations*.json')):
            for conv in _safe(lambda: list(_chatgpt_conversations(path))) or []:
                if not conv['messages'] or not conv['id']:
                    continue
                summary = _summary('chatgpt', conv['id'], conv['title'], conv['updated'], conv['messages'], 'ChatGPT', secret_re)
                index[summary['id']] = ('chatgpt', path)
                items.append(summary)
    items.sort(key=lambda x: x['updated'], reverse=True)
    return items[:LIST_LIMIT], index


def _safe(build):
    try:
        return build()
    except (OSError, ValueError):
        return None


def list_chats(secret_re):
    with _lock:
        items, index = _scan(secret_re)
        _index.clear()
        _index.update(index)
    return dict(chats=items, sources=dict(claude=CLAUDE_DIR.is_dir(), codex=CODEX_DIR.is_dir(), chatgpt=CHATGPT_DIR.is_dir()))


def read_chat(chat_id, secret_re):
    """Return one chat's messages, redacted and capped. Unknown or malformed ids give None."""
    source, _, raw = chat_id.partition(':')
    if source not in ('claude', 'codex', 'chatgpt') or not ID_RE.fullmatch(raw):
        return None
    with _lock:
        entry = _index.get(chat_id)
        if entry is None:
            _index.update(_scan(secret_re)[1])
            entry = _index.get(chat_id)
    if entry is None:
        return None
    kind, path = entry
    if kind == 'file':
        messages = _claude_messages(path) if source == 'claude' else _codex_messages(path)
        title = ''
    else:
        conv = next((c for c in _safe(lambda: list(_chatgpt_conversations(path))) or [] if c['id'] == raw), None)
        if conv is None:
            return None
        messages, title = conv['messages'], conv['title']
    shown = []
    for m in messages[-MESSAGE_LIMIT:]:
        text = secret_re.sub('***', m['text'])
        shown.append(dict(role=m['role'], text=text[:TEXT_LIMIT] + ('…' if len(text) > TEXT_LIMIT else ''), time=m['time']))
    head = secret_re.sub('***', title or next((m['text'] for m in messages if m['role'] == 'user'), ''))
    return dict(id=chat_id, source=source, title=_title(head), messages=shown,
                hidden=max(0, len(messages) - MESSAGE_LIMIT))
