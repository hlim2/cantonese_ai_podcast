#!/usr/bin/env python3
"""Synthesize a multi-speaker Cantonese podcast MP3 via OpenRouter ElevenLabs TTS."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# OpenRouter ElevenLabs models accept up to ~10k characters per request.
MAX_CHUNK_CHARS = int(os.getenv("OPENROUTER_TTS_MAX_CHUNK_CHARS", "4000"))
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
OPENROUTER_TTS_MODEL = os.getenv(
    "OPENROUTER_TTS_MODEL", "elevenlabs/eleven-v4-turbo"
)
OPENROUTER_TTS_FORMAT = os.getenv("OPENROUTER_TTS_FORMAT", "mp3").strip() or "mp3"

# Published voices for elevenlabs/eleven-v4-turbo on OpenRouter.
# Opaque ElevenLabs dashboard IDs (e.g. pyCpQJNpFBywhVAwOzn6) are NOT accepted.
OPENROUTER_VOICES = frozenset(
    {
        "george",
        "sarah",
        "adam",
        "alice",
        "bella",
        "bill",
        "brian",
        "callum",
        "charlie",
        "chris",
        "daniel",
        "eric",
        "harry",
        "jessica",
        "laura",
        "liam",
        "lily",
        "matilda",
        "river",
        "roger",
        "will",
    }
)


def _resolve_named_voice(*env_keys: str, default: str) -> str:
    for key in env_keys:
        raw = (os.getenv(key) or "").strip().lower()
        if raw in OPENROUTER_VOICES:
            return raw
    return default


SPEAKER_VOICES = {
    "阿希": _resolve_named_voice(
        "OPENROUTER_VOICE_AH_HEI",
        "VOICE_AH_HEI",
        default="sarah",
    ),
    "阿明": _resolve_named_voice(
        "OPENROUTER_VOICE_AH_MING",
        "VOICE_AH_MING",
        default="george",
    ),
}

LINE_RE = re.compile(r"^(阿希|阿明)\s*[:：]\s*(.+)$")
UNSAFE_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def log(message: str) -> None:
    print(message, file=sys.stderr)


# #region agent log
def _agent_dbg(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    payload = {
        "sessionId": "840db8",
        "runId": os.getenv("GITHUB_RUN_ID", "local"),
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, ensure_ascii=False)
    print(f"DEBUG_840db8 {line}", file=sys.stderr)
    try:
        with open("debug-840db8.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# #endregion


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


def sanitize_tts_text(text: str) -> str:
    cleaned = UNSAFE_CHARS_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def require_config() -> str:
    api_key = (
        os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY") or ""
    ).strip()
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is required for OpenRouter TTS "
            f"(model={OPENROUTER_TTS_MODEL})"
        )
    return api_key


def chunk_dialogue(
    dialogue: list[tuple[str, str]],
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[dict[str, Any]]:
    """Group consecutive same-speaker lines into OpenRouter speech requests.

    ElevenLabs via OpenRouter does not accept multi-speaker turn arrays, so each
    chunk is a single voice + concatenated text.
    """
    chunks: list[dict[str, Any]] = []
    current_speaker: str | None = None
    current_texts: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current_speaker, current_texts, current_chars
        if current_speaker and current_texts:
            chunks.append(
                {
                    "speaker": current_speaker,
                    "voice": SPEAKER_VOICES[current_speaker],
                    "text": " ".join(current_texts),
                    "line_count": len(current_texts),
                }
            )
        current_speaker = None
        current_texts = []
        current_chars = 0

    for speaker, content in dialogue:
        text = sanitize_tts_text(content)
        if not text:
            continue
        text_len = len(text)
        same_speaker = speaker == current_speaker
        would_overflow = current_texts and current_chars + 1 + text_len > max_chars
        if current_texts and (not same_speaker or would_overflow):
            flush()

        current_speaker = speaker
        current_texts.append(text)
        current_chars += text_len if len(current_texts) == 1 else 1 + text_len

        if current_chars >= max_chars:
            flush()

    flush()
    return chunks


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:500]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:500]
        detail = payload.get("detail") or payload.get("message") or payload
        return str(detail)[:500]
    return str(payload)[:500]


def synthesize_chunk(
    api_key: str,
    *,
    text: str,
    voice: str,
    output_path: Path,
    max_attempts: int = 4,
) -> str | None:
    """Call OpenRouter /audio/speech and write mp3 bytes. Returns generation id."""
    url = f"{OPENROUTER_BASE_URL}/audio/speech"
    body: dict[str, Any] = {
        "model": OPENROUTER_TTS_MODEL,
        "input": text,
        "voice": voice,
        "response_format": OPENROUTER_TTS_FORMAT,
        "provider": {"only": ["elevenlabs"]},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "audio/mpeg, application/json",
        "HTTP-Referer": os.getenv(
            "OPENROUTER_HTTP_REFERER",
            "https://github.com/hlim2/cantonese_ai_podcast",
        ),
        "X-Title": os.getenv("OPENROUTER_APP_TITLE", "cantonese_ai_podcast"),
    }

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=180,
            )
            content_type = (response.headers.get("Content-Type") or "").lower()
            if (
                response.status_code == 200
                and response.content
                and "application/json" not in content_type
            ):
                output_path.write_bytes(response.content)
                return (
                    response.headers.get("X-Generation-Id")
                    or response.headers.get("x-generation-id")
                )

            detail = _error_detail(response)
            if response.status_code in {408, 429, 500, 502, 503, 504}:
                last_error = RuntimeError(
                    f"OpenRouter TTS HTTP {response.status_code}: {detail}"
                )
                log(
                    f"TTS chunk retry {attempt}/{max_attempts} "
                    f"HTTP {response.status_code}: {detail}"
                )
                time.sleep(1.5 * attempt)
                continue

            raise RuntimeError(
                f"OpenRouter TTS HTTP {response.status_code}: {detail}"
            )
        except requests.RequestException as exc:
            last_error = exc
            log(f"TTS chunk network error attempt {attempt}/{max_attempts}: {exc}")
            if attempt < max_attempts:
                time.sleep(1.5 * attempt)

    raise RuntimeError(
        f"OpenRouter TTS failed after {max_attempts} attempts "
        f"for voice={voice} chars={len(text)}"
    ) from last_error


def concat_with_ffmpeg(parts: list[Path], output_mp3: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required but was not found on PATH")

    if len(parts) == 1:
        shutil.copyfile(parts[0], output_mp3)
        return

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


def build_podcast(script_path: Path, output_mp3: Path) -> None:
    api_key = require_config()
    script_text = script_path.read_text(encoding="utf-8")
    dialogue = parse_dialogue(script_text)
    if not dialogue:
        raise RuntimeError(
            "No dialogue lines found. Expected lines like '阿希: ...' / '阿明: ...'"
        )

    chunks = chunk_dialogue(dialogue)
    if not chunks:
        raise RuntimeError("No synthesizable text after sanitization")

    log(
        f"Synthesizing {len(dialogue)} dialogue lines in {len(chunks)} "
        f"OpenRouter chunk(s) model={OPENROUTER_TTS_MODEL}"
    )
    _agent_dbg(
        "TTS-A",
        "synthesize_podcast.py:build:start",
        "dialogue parsed",
        {
            "line_count": len(dialogue),
            "chunk_count": len(chunks),
            "model": OPENROUTER_TTS_MODEL,
            "voices": SPEAKER_VOICES,
            "base_url": OPENROUTER_BASE_URL,
        },
    )

    with tempfile.TemporaryDirectory(prefix="podcast-tts-") as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []

        for idx, chunk in enumerate(chunks, start=1):
            part_path = tmp_dir / f"part_{idx:04d}.mp3"
            log(
                f"[{idx}/{len(chunks)}] speaker={chunk['speaker']} "
                f"voice={chunk['voice']} lines={chunk['line_count']} "
                f"chars={len(chunk['text'])} model={OPENROUTER_TTS_MODEL}"
            )
            _agent_dbg(
                "TTS-A,TTS-B",
                "synthesize_podcast.py:chunk:pre",
                "about to synthesize chunk",
                {
                    "idx": idx,
                    "total": len(chunks),
                    "speaker": chunk["speaker"],
                    "voice": chunk["voice"],
                    "lines": chunk["line_count"],
                    "chars": len(chunk["text"]),
                    "preview": chunk["text"][:80],
                },
            )
            synthesize_chunk(
                api_key,
                text=chunk["text"],
                voice=chunk["voice"],
                output_path=part_path,
            )
            parts.append(part_path)
            if idx < len(chunks):
                time.sleep(0.35)

        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        concat_with_ffmpeg(parts, output_mp3)
        log(f"Wrote {output_mp3}")
        _agent_dbg(
            "TTS-A",
            "synthesize_podcast.py:build:done",
            "podcast written",
            {"output": str(output_mp3), "parts": len(parts)},
        )


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

    build_podcast(script_path, Path(args.output))


if __name__ == "__main__":
    main()
