#!/usr/bin/env python3
"""Background job status for the Svodka office: one atomic JSON file per job.

    office-job start <id> "<title>" [--kind video] [--total 46] [--parent <id>] [--detail "..."]
    office-job progress <id> [--done 8] [--total 46] [--detail "..."]
    office-job done <id> [--detail "..."]
    office-job fail <id> [--detail "..."]

Bad arguments exit with 2; write errors only print to stderr and exit 0 so the calling script goes on.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from office import JOB_ID, jobs_dir, read_job_file  # noqa: E402

STATUS = {'start': 'running', 'progress': 'running', 'done': 'done', 'fail': 'failed'}


def job_id(value):
    if not JOB_ID.fullmatch(value):
        raise argparse.ArgumentTypeError('id: латиница, цифры, «_», «-», «.», до 64 символов, без путей')
    return value


def number(value):
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('нужно целое число') from None
    if n < 0:
        raise argparse.ArgumentTypeError('нужно число не меньше нуля')
    return n


def parser():
    p = argparse.ArgumentParser(prog='office-job', description='Статус фоновой работы для офиса «Сводки».')
    sub = p.add_subparsers(dest='command', required=True)
    for name in STATUS:
        c = sub.add_parser(name)
        c.add_argument('id', type=job_id)
        if name == 'start':
            c.add_argument('title')
            c.add_argument('--kind')
            c.add_argument('--parent', type=job_id)
        if name in ('start', 'progress'):
            c.add_argument('--total', type=number)
        if name == 'progress':
            c.add_argument('--done', type=number)
        c.add_argument('--detail')
    return p


def update(directory, args, now=None):
    """Merge the command into <id>.json and replace it atomically; returns the written job."""
    now = time.time() if now is None else now
    directory = Path(directory)
    path = directory / (args.id + '.json')
    old = read_job_file(path) or {}
    if old.get('id') != args.id:
        old = {}
    job = {} if args.command == 'start' else dict(old)
    job.update(id=args.id, status=STATUS[args.command], updated=now)
    job.setdefault('title', args.id)
    job.setdefault('started', now)
    if args.command == 'start':
        job['title'] = args.title[:200]
        for key in ('kind', 'parent'):
            if getattr(args, key):
                job[key] = getattr(args, key)[:64]
    if getattr(args, 'total', None) is not None:
        job['progress_total'] = args.total
    if getattr(args, 'done', None) is not None:
        job['progress_done'] = args.done
    elif args.command == 'done' and isinstance(job.get('progress_total'), int):
        job['progress_done'] = job['progress_total']
    if args.detail is not None:
        job['detail'] = args.detail[:500]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + args.id + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(job, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return job


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        update(jobs_dir(), args)
    except Exception as e:  # never break the calling script
        print('office-job: не удалось записать статус: ' + str(e), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
