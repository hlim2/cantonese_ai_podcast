#!/usr/bin/env python3
"""Synthesize a multi-speaker Cantonese podcast MP3 with ElevenLabs + ffmpeg."""

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

# ElevenLabs Text-to-Dialogue: keep total input text <= 2000 chars per request.
MAX_CHUNK_CHARS = int(os.getenv("ELEVENLABS_MAX_CHUNK_CHARS", "1800"))
ELEVENLABS_BASE_URL = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_v4_turbo")
ELEVENLABS_OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
ELEVENLABS_LANGUAGE_CODE = os.getenv("ELEVENLABS_LANGUAGE_CODE", "").strip()

SPEAKER_VOICES = {
    "阿希": (
        os.getenv("ELEVENLABS_VOICE_AH_HEI")
        or os.getenv("VOICE_AH_HEI")
        or ""
    ).strip(),
    "阿明": (
        os.getenv("ELEVENLABS_VOICE_AH_MING")
        or os.getenv("VOICE_AH_MING")
        or ""
    ).strip(),
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
    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is required for ElevenLabs TTS "
            f"(model={ELEVENLABS_MODEL})"
        )
    missing = [name for name, voice in SPEAKER_VOICES.items() if not voice]
    if missing:
        raise RuntimeError(
            "Missing ElevenLabs voice IDs for: "
            + ", ".join(missing)
            + ". Set ELEVENLABS_VOICE_AH_HEI / ELEVENLABS_VOICE_AH_MING "
            "(or VOICE_AH_HEI / VOICE_AH_MING)."
        )
    return api_key


def chunk_dialogue(
    dialogue: list[tuple[str, str]],
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[list[dict[str, str]]]:
    """Group dialogue lines into Text-to-Dialogue request payloads."""
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_chars = 0

    for speaker, content in dialogue:
        text = sanitize_tts_text(content)
        if not text:
            continue
        voice_id = SPEAKER_VOICES[speaker]
        item = {"text": text, "voice_id": voice_id}
        item_len = len(text)

        if current and current_chars + item_len > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0

        # A single oversized line still gets its own request.
        current.append(item)
        current_chars += item_len

        if current_chars >= max_chars:
            chunks.append(current)
            current = []
            current_chars = 0

    if current:
        chunks.append(current)
    return chunks


def _parse_error_payload(response: requests.Response) -> dict[str, Any] | str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:500]
    if isinstance(payload, dict):
        return payload
    return str(payload)[:500]


def _error_detail(response: requests.Response) -> str:
    payload = _parse_error_payload(response)
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload
        return str(detail)[:500]
    return payload


def _format_elevenlabs_http_error(response: requests.Response) -> str:
    """Build an actionable error for common ElevenLabs account/voice failures."""
    payload = _parse_error_payload(response)
    detail = _error_detail(response)
    base = f"ElevenLabs HTTP {response.status_code}: {detail}"
    if not isinstance(payload, dict):
        return base

    status = str(payload.get("status") or payload.get("code") or "").lower()
    message = str(payload.get("message") or "").lower()
    if status in {"free_users_not_allowed", "voice_not_allowed"} or (
        "creator tier" in message or "free users" in message
    ):
        return (
            f"{base}\n"
            "Fix: this voice requires ElevenLabs Creator (or higher). Either "
            "upgrade the account, or set ELEVENLABS_VOICE_AH_HEI / "
            "ELEVENLABS_VOICE_AH_MING to free Voice Library IDs that your plan "
            "can use."
        )
    if status in {"invalid_api_key", "unauthorized"} or response.status_code == 401:
        return (
            f"{base}\n"
            "Fix: update GitHub secret ELEVENLABS_API_KEY with a valid key "
            "(no quotes/spaces)."
        )
    return base


def synthesize_chunk(
    api_key: str,
    inputs: list[dict[str, str]],
    output_path: Path,
    *,
    previous_request_ids: list[str] | None = None,
    max_attempts: int = 4,
) -> str | None:
    """Call ElevenLabs Text-to-Dialogue and write mp3 bytes. Returns request-id."""
    url = f"{ELEVENLABS_BASE_URL}/v1/text-to-dialogue"
    params = {"output_format": ELEVENLABS_OUTPUT_FORMAT}
    body: dict[str, Any] = {
        "inputs": inputs,
        "model_id": ELEVENLABS_MODEL,
    }
    if ELEVENLABS_LANGUAGE_CODE:
        body["language_code"] = ELEVENLABS_LANGUAGE_CODE
    if previous_request_ids:
        body["previous_request_ids"] = previous_request_ids[-3:]

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                url,
                params=params,
                headers=headers,
                json=body,
                timeout=180,
            )
            if response.status_code == 200 and response.content:
                output_path.write_bytes(response.content)
                request_id = (
                    response.headers.get("request-id")
                    or response.headers.get("x-request-id")
                )
                return request_id

            detail = _format_elevenlabs_http_error(response)
            # Retry transient / rate-limit responses.
            if response.status_code in {408, 429, 500, 502, 503, 504}:
                last_error = RuntimeError(detail)
                log(
                    f"TTS chunk retry {attempt}/{max_attempts} "
                    f"HTTP {response.status_code}: {_error_detail(response)}"
                )
                time.sleep(1.5 * attempt)
                continue

            raise RuntimeError(detail)
        except requests.RequestException as exc:
            last_error = exc
            log(f"TTS chunk network error attempt {attempt}/{max_attempts}: {exc}")
            if attempt < max_attempts:
                time.sleep(1.5 * attempt)

    raise RuntimeError(
        f"ElevenLabs TTS failed after {max_attempts} attempts "
        f"for {len(inputs)} lines / "
        f"{sum(len(i['text']) for i in inputs)} chars"
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
        f"ElevenLabs chunk(s) model={ELEVENLABS_MODEL}"
    )
    _agent_dbg(
        "TTS-A",
        "synthesize_podcast.py:build:start",
        "dialogue parsed",
        {
            "line_count": len(dialogue),
            "chunk_count": len(chunks),
            "model": ELEVENLABS_MODEL,
            "voices": {k: (v[:6] + "…") if v else "" for k, v in SPEAKER_VOICES.items()},
            "base_url": ELEVENLABS_BASE_URL,
        },
    )

    with tempfile.TemporaryDirectory(prefix="podcast-tts-") as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        previous_ids: list[str] = []

        for idx, inputs in enumerate(chunks, start=1):
            part_path = tmp_dir / f"part_{idx:04d}.mp3"
            char_count = sum(len(item["text"]) for item in inputs)
            log(
                f"[{idx}/{len(chunks)}] lines={len(inputs)} chars={char_count} "
                f"model={ELEVENLABS_MODEL}"
            )
            _agent_dbg(
                "TTS-A,TTS-B",
                "synthesize_podcast.py:chunk:pre",
                "about to synthesize chunk",
                {
                    "idx": idx,
                    "total": len(chunks),
                    "lines": len(inputs),
                    "chars": char_count,
                    "preview": inputs[0]["text"][:80],
                },
            )
            request_id = synthesize_chunk(
                api_key,
                inputs,
                part_path,
                previous_request_ids=previous_ids or None,
            )
            if request_id:
                previous_ids.append(request_id)
            parts.append(part_path)
            # Light pacing between chunks to reduce rate-limit pressure.
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
