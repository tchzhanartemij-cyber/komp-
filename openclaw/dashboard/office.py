"""Read-only office metadata. Session assembly and stream parsing are pure."""
import hashlib
import json
import math
import os
import re
import shlex
from datetime import datetime
from pathlib import Path

LOG_LIMIT = 256 * 1024
TOOLS = {**dict.fromkeys(('Edit', 'Write', 'MultiEdit', 'NotebookEdit'), 'code'),
         **dict.fromkeys(('Read', 'Grep', 'Glob'), 'search_code'),
         **dict.fromkeys(('WebSearch', 'WebFetch'), 'web'),
         'Bash': 'command', 'Task': 'subagent', 'Agent': 'subagent'}


def model_name(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,100}', value) else None


def redact(value):
    text = value if isinstance(value, str) else ''
    text = re.sub(r'(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+', '[ссылка]', text)
    text = re.sub(r'(?i)\S*(?:sk-|token|key=)\S*', '[скрыто]', text)
    text = re.sub(r'[A-Za-z0-9_-]{24,}', '[скрыто]', text)
    return ' '.join(text.split())


def task_title(value):
    text = value.splitlines()[0] if isinstance(value, str) and value else ''
    # Queue texts are long prompts: keep the first sentence without labels and code paths.
    text = re.sub(r'^\s*(?:ЗАДАЧА|Задача)\s*(?:№?\d+)?\s*[:.]\s*', '', text)
    text = re.sub(r'\s*\([^()]*[/_][^()]*\)', '', text)
    text = re.split(r'(?<=[^\s.]{3})[.!?](?:\s|$)', text, maxsplit=1)[0]
    text = redact(text)
    text = text[:1].upper() + text[1:]
    return text if len(text) <= 60 else text[:59].rstrip(' ,;:-') + '…'


def program_name(command):
    """Leading program of a shell command (after cd/env/assignments), never its arguments."""
    if not isinstance(command, str):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        args = list(lexer)
    except ValueError:
        return None
    while args[:1] == ['cd']:
        end = next((i for i, a in enumerate(args) if a in ('&&', ';')), len(args))
        args = args[end + 1:]
    if args[:1] == ['env']:
        args = args[1:]
    while args and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', args[0]):
        args = args[1:]
    name = os.path.basename(args[0]) if args else ''
    return name if re.fullmatch(r'[A-Za-z0-9._+-]{1,24}', name) else None


def tool_detail(name, inp):
    if not isinstance(inp, dict):
        return None
    if name in ('Edit', 'Write', 'MultiEdit', 'Read', 'NotebookEdit'):
        path = inp.get('file_path') or inp.get('notebook_path')
        base = os.path.basename(path) if isinstance(path, str) else ''
        return base if re.fullmatch(r'[\w.+-]{1,48}', base) and redact(base) == base else None
    if name == 'Bash':
        return program_name(inp.get('command'))
    return None


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            # Naive coder stamps ("%Y-%m-%d %H:%M") are this host's local time.
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def codex_command(command):
    if not isinstance(command, str):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        args = list(lexer)
    except ValueError:
        return None
    # Recognize only a leading invocation, optionally after cd/env/assignments.
    if args[:1] == ['cd']:
        end = next((i for i, a in enumerate(args) if a in ('&&', ';')), len(args))
        if end != 2 and not (end == 3 and args[1] == '--'):
            return None
        args = args[end + 1:]
    if args[:1] == ['env']:
        args = args[1:]
    while args and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', args[0]):
        args = args[1:]
    if args[:1] != ['codex']:
        return None
    model = None
    for i, arg in enumerate(args[1:], 1):
        if arg in (';', '&&', '||', '|'):
            break
        if arg in ('-m', '--model') and i + 1 < len(args):
            model = model_name(args[i + 1])
        elif arg.startswith('--model='):
            model = model_name(arg[8:])
    return {'model': model}


def parse_coder_log(data, at):
    activity, detail, pending, open_tools = None, None, {}, set()
    for line in data.splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(obj, dict) or obj.get('type') not in ('assistant', 'user'):
            continue
        message = obj.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if obj['type'] == 'assistant' and kind == 'tool_use':
                name = item.get('name')
                activity = TOOLS.get(name, 'tool') if isinstance(name, str) else 'tool'
                inp, tid = item.get('input'), item.get('id')
                detail = tool_detail(name, inp)
                if isinstance(tid, str):
                    open_tools.add(tid)
                child = codex_command(inp.get('command')) if name == 'Bash' and isinstance(inp, dict) else None
                if child is not None and isinstance(tid, str):
                    pending[tid] = child
            elif obj['type'] == 'assistant' and kind in ('text', 'thinking'):
                if item.get(kind):
                    activity, detail = 'thinking', None
            elif obj['type'] == 'user' and kind == 'tool_result':
                tid = item.get('tool_use_id')
                if isinstance(tid, str):
                    pending.pop(tid, None)
                    open_tools.discard(tid)
    # An unanswered tool call (e.g. a long codex exec) is confirmed ongoing work.
    return {'activity': activity, 'detail': detail, 'activity_at': timestamp(at), 'tool_pending': bool(open_tools),
            'codex': next(reversed(pending.values())) if pending else None}


def coder_metadata(row, logs_dir):
    result = {'title': task_title(row.get('text')), 'activity': None, 'detail': None, 'activity_at': None, 'tool_pending': False, 'codex': None}
    rid = str(row.get('id', ''))
    if row.get('status') != 'running' or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', rid):
        return result
    try:
        files = [p for p in Path(logs_dir).glob(rid + '-*.json') if not p.is_symlink() and p.is_file()]
        if not files:
            return result
        newest = max(files, key=lambda p: p.stat().st_mtime)
        with newest.open('rb') as f:
            stat = os.fstat(f.fileno())
            offset = max(0, stat.st_size - LOG_LIMIT)
            f.seek(offset)
            data = f.read(LOG_LIMIT)
        if offset:
            data = data.partition(b'\n')[2]
        result.update(parse_coder_log(data, stat.st_mtime))
    except (OSError, ValueError):
        pass
    return result


def office_sessions(agent, queue, chats):
    sessions = []
    def session(key, kind, label, title, provider, row, activity=None, at=None, parent=None, detail=None):
        return dict(key=key, kind=kind, label=label, title=title, provider=provider,
                    model=model_name(row.get('model')), status=row.get('status', 'unknown'),
                    activity=activity, detail=detail, activity_at=timestamp(at), tool_pending=row.get('tool_pending') is True,
                    started=timestamp(row.get('started', row.get('created'))),
                    updated=timestamp(row.get('updated')), parent=parent)
    for row in agent.get('runs', []):
        rid = row['id']
        events = [e for e in agent.get('events', []) if e.get('run') == rid]
        latest = max(events, key=lambda e: timestamp(e.get('time')) or 0, default={})
        activity = ('answering' if (latest.get('output_chars') or 0) > 0 else 'waiting_model') if row.get('status') == 'running' else None
        sessions.append(session('agent:' + str(rid), 'agent', 'Сводка #' + str(rid),
                                task_title(row.get('input')), 'Сводка-агент', row, activity, latest.get('time')))
    for row in queue.get('jobs', []):
        rid = str(row.get('id', ''))
        key = 'coder:' + rid
        running = row.get('status') == 'running'
        activity = row.get('activity') if running else None
        at = row.get('activity_at') if running else None
        # Keep status updates even if the last observed action predates them.
        observed = {**row, 'model': None}
        if running and timestamp(at) is not None:
            observed['updated'] = max(timestamp(at), timestamp(row.get('updated')) or 0)
        sessions.append(session(key, 'coder', 'Кодер #' + rid, row.get('title') or task_title(row.get('text')),
                                'Claude Code', observed, activity, at, detail=row.get('detail') if running else None))
        child = row.get('codex')
        if running and isinstance(child, dict):
            sessions.append(session(key + ':codex', 'codex', 'Codex · кодер #' + rid,
                                    row.get('title') or task_title(row.get('text')), 'Codex',
                                    {**observed, 'model': child.get('model')}, 'code', at, key))
    for rid, row in chats.items():
        provider = row.get('provider')
        if provider not in ('Claude', 'ChatGPT', 'Бесплатная'):
            continue
        key = 'chat:' + hashlib.sha256(str(rid).encode()).hexdigest()[:12]
        sessions.append(session(key, 'chat', 'Чат · ' + provider, 'Диалог в ИИ', provider,
                                {**row, 'status': 'running', 'model': None},
                                'answering' if row.get('phase') == 'streaming' else 'waiting_model', row.get('updated')))
    return sessions
