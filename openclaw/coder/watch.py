#!/usr/bin/env python3
"""Смотрит видео через Gemini и описывает интерфейс и анимации словами.

  watch.py <видеофайл> ["на что смотреть"]

Печатает описание, а затем пути к 3–5 ключевым кадрам, которые выбрал Gemini
(строки «КАДР: путь»). Описание и кадры идут в `coder.py add --ref ...`.
Ключ: ~/.config/jarvis/gemini-key. Модель можно сменить в GEMINI_MODEL.
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

KEY_FILE = Path.home() / ".config/jarvis/gemini-key"
API = "https://generativelanguage.googleapis.com"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
INLINE_LIMIT = 18 * 1024 * 1024   # до этого размера видео идёт прямо в запросе

PROMPT = """Это видео-образец: владелец хочет похожий интерфейс у себя (дашборд или сайт).
Опиши его так, чтобы программист повторил без видео. По-русски, по пунктам:

1. Общий вид: что за экран, сетка и расположение блоков, отступы, скругления, тени.
2. Цвета (примерные hex), шрифты (с засечками или без, жирность, размеры относительно друг друга).
3. Каждая анимация отдельно: что движется, откуда и куда, примерная длительность в секундах,
   плавность (резко, плавно, с отскоком), задержки между элементами, что её запускает
   (открытие экрана, прокрутка, наведение, нажатие).
4. Интерактив: что происходит при нажатиях и переходах между экранами.
5. Если видно название библиотеки, сервиса или шаблона, назови его.

Не выдумывай то, чего не видно. Если что-то неразборчиво, так и скажи.
{focus}
Последней строкой выведи 3–5 моментов, кадры из которых лучше всего показывают дизайн:
КАДРЫ: мм:сс, мм:сс, мм:сс"""


def key():
    if not KEY_FILE.exists():
        sys.exit(f"Нет ключа Gemini в {KEY_FILE}.")
    return KEY_FILE.read_text().strip()


def call(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-goog-api-key": key(), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.headers, r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        sys.exit(f"Gemini ответил ошибкой {e.code}: {body[:500]}")


def upload(path, mime):
    """Большое видео загружаем через Files API и ждём, пока Google его обработает."""
    size = path.stat().st_size
    headers, _ = call(f"{API}/upload/v1beta/files", method="POST",
                      data=json.dumps({"file": {"display_name": path.name}}).encode(),
                      headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                               "X-Goog-Upload-Header-Content-Length": str(size),
                               "X-Goog-Upload-Header-Content-Type": mime,
                               "Content-Type": "application/json"})
    _, body = call(headers["x-goog-upload-url"], data=path.read_bytes(), method="POST",
                   headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                            "Content-Length": str(size)})
    f = json.loads(body)["file"]
    for _ in range(60):
        if f.get("state") == "ACTIVE":
            return {"file_data": {"mime_type": mime, "file_uri": f["uri"]}}
        if f.get("state") == "FAILED":
            sys.exit("Gemini не смог обработать видео.")
        time.sleep(5)
        _, body = call(f"{API}/v1beta/{f['name']}")
        f = json.loads(body)
    sys.exit("Gemini слишком долго обрабатывает видео.")


def describe(path, focus):
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    if path.stat().st_size <= INLINE_LIMIT:
        import base64
        media = {"inline_data": {"mime_type": mime, "data": base64.b64encode(path.read_bytes()).decode()}}
    else:
        media = upload(path, mime)
    focus = f"\nОсобенно важно владельцу: {focus}\n" if focus else ""
    req = {"contents": [{"parts": [media, {"text": PROMPT.format(focus=focus)}]}]}
    _, body = call(f"{API}/v1beta/models/{MODEL}:generateContent", data=json.dumps(req).encode(),
                   method="POST", headers={"Content-Type": "application/json"})
    res = json.loads(body)
    try:
        return "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"]).strip()
    except (KeyError, IndexError):
        sys.exit(f"Gemini не дал ответа: {json.dumps(res, ensure_ascii=False)[:500]}")


def key_frames(path, text, out):
    m = re.search(r"КАДРЫ:\s*(.+)\s*$", text)
    if not m:
        return []
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, stamp in enumerate(re.findall(r"(\d{1,2}):(\d{2})", m[1])[:5], 1):
        sec = int(stamp[0]) * 60 + int(stamp[1])
        dest = out / f"key-{i}-{stamp[0]}m{stamp[1]}s.jpg"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(sec), "-i", str(path), "-frames:v", "1",
                            "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(dest)], capture_output=True)
        if r.returncode == 0 and dest.exists():
            frames.append(dest)
    return frames


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1]).expanduser()
    if not path.is_file():
        sys.exit(f"Нет файла {path}")
    text = describe(path, " ".join(sys.argv[2:]).strip())
    print(re.sub(r"\n?КАДРЫ:.*$", "", text).strip())
    out = Path.home() / ".openclaw/workspace/coder/frames" / datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        frames = key_frames(path, text, out)
    except FileNotFoundError:
        frames = []
        print("\n(нет ffmpeg, кадры не вырезаны)")
    for f in frames:
        print(f"КАДР: {f}")


if __name__ == "__main__":
    main()
