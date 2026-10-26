#!/bin/bash
# Раскладывает видео на кадры, чтобы приложить их к задаче как образец.
#
#   bash frames.sh <видеофайл> [папка] [сколько кадров, по умолчанию 16]
#
# Печатает пути к кадрам, по одному на строку: их можно сразу передать в
# `coder.py add --ref ...`. Кадры берутся равномерно по всей длине видео.
set -euo pipefail

VIDEO="${1:?Укажи видеофайл}"
OUT="${2:-$HOME/.openclaw/workspace/coder/frames/$(date +%Y%m%d-%H%M%S)}"
COUNT="${3:-16}"

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "Нет ffmpeg. Поставь под root: apt install -y ffmpeg" >&2
  exit 1
fi
[ -f "$VIDEO" ] || { echo "Нет файла $VIDEO" >&2; exit 1; }

DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" | cut -d. -f1)"
DUR="${DUR:-0}"
[ "$DUR" -ge 1 ] 2>/dev/null || DUR=1
mkdir -p "$OUT"
# Кадр в секунду не чаще одного; ширина 1080, чтобы были видны шрифты и мелкие детали.
FPS="$(python3 -c "print(min(1.0, $COUNT / $DUR))")"
ffmpeg -v error -y -i "$VIDEO" -vf "fps=$FPS,scale='min(1080,iw)':-2" -frames:v "$COUNT" -q:v 3 "$OUT/frame-%02d.jpg"
ls -1 "$OUT"/frame-*.jpg
