#!/usr/bin/env python3
"""Synthesize a multi-speaker Cantonese podcast MP3 with edge-tts + ffmpeg."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import edge_tts

SPEAKER_VOICES = {
    "阿希": os.getenv("VOICE_AH_HEI", "zh-HK-HiuMaanNeural"),
    "阿明": os.getenv("VOICE_AH_MING", "zh-HK-WanLungNeural"),
}

LINE_RE = re.compile(r"^(阿希|阿明)\s*[:：]\s*(.+)$")


def log(message: str) -> None:
    print(message, file=sys.stderr)


def parse_dialogue(script_text: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for raw in script_text.splitlines():
        text = raw.strip()
        if not text:
            continue
        match = LINE_RE.match(text)
        if not match:
            continue
        speaker, content = match.group(1), match.group(2).strip()
        if content:
            lines.append((speaker, content))
    return lines


async def synthesize_line(text: str, voice: str, output_path: Path) -> None:
    communicate = edge_tts.Communicate(text, voice=voice)
    await communicate.save(str(output_path))


def concat_with_ffmpeg(parts: list[Path], output_mp3: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required but was not found on PATH")

    list_file = output_mp3.with_suffix(".txt")
    list_file.write_text(
        "\n".join(f"file '{part.resolve().as_posix()}'" for part in parts) + "\n",
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:a",
        "libmp3lame",
        "-q:a",
        "4",
        str(output_mp3),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")


async def build_podcast(script_path: Path, output_mp3: Path) -> None:
    script_text = script_path.read_text(encoding="utf-8")
    dialogue = parse_dialogue(script_text)
    if not dialogue:
        raise RuntimeError(
            "No dialogue lines found. Expected lines like '阿希: ...' / '阿明: ...'"
        )

    log(f"Synthesizing {len(dialogue)} dialogue lines")
    with tempfile.TemporaryDirectory(prefix="podcast-tts-") as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        for idx, (speaker, content) in enumerate(dialogue):
            voice = SPEAKER_VOICES[speaker]
            part_path = tmp_dir / f"part_{idx:04d}.mp3"
            log(f"[{idx + 1}/{len(dialogue)}] {speaker} ({voice})")
            await synthesize_line(content, voice, part_path)
            parts.append(part_path)

        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        concat_with_ffmpeg(parts, output_mp3)
        log(f"Wrote {output_mp3}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesize podcast MP3")
    parser.add_argument("--script", default="podcast_script.md")
    parser.add_argument(
        "--output",
        default=f"AI_Daily_Podcast_{datetime.now(timezone.utc).strftime('%Y%m%d')}.mp3",
    )
    args = parser.parse_args()

    script_path = Path(args.script)
    if not script_path.exists():
        raise SystemExit(f"Script file not found: {script_path}")

    asyncio.run(build_podcast(script_path, Path(args.output)))


if __name__ == "__main__":
    main()
