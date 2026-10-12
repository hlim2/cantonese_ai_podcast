#!/usr/bin/env python3
"""Synthesize a multi-speaker Cantonese podcast MP3 with edge-tts + ffmpeg."""

from __future__ import annotations

import argparse
import asyncio
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

import edge_tts
import edge_tts.communicate as edge_communicate
from aiohttp import ClientResponseError
from edge_tts.constants import TRUSTED_CLIENT_TOKEN, WSS_URL as LEGACY_WSS_URL
from edge_tts.exceptions import NoAudioReceived

SPEAKER_VOICES = {
    "阿希": os.getenv("VOICE_AH_HEI", "zh-HK-HiuMaanNeural"),
    "阿明": os.getenv("VOICE_AH_MING", "zh-HK-WanLungNeural"),
}

# Microsoft moved Edge Read Aloud synthesis off speech.platform.bing.com (403
# worldwide as of 2026-10-12). The Edge 155 host still accepts the legacy
# Sec-MS-GEC token when passed as Ocp-Apim-Subscription-Key. See edge-tts#490.
NEW_WSS_URL = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"
)
TTS_WSS_URLS = (
    os.getenv("EDGE_TTS_WSS_URL", "").strip() or NEW_WSS_URL,
    LEGACY_WSS_URL,
)

# Slightly slower / warmer pacing for less mechanical delivery.
TTS_RATE = os.getenv("TTS_RATE", "-8%")
TTS_PITCH = os.getenv("TTS_PITCH", "+0Hz")

LINE_RE = re.compile(r"^(阿希|阿明)\s*[:：]\s*(.+)$")
# Characters that often break Edge TTS / SSML parsing.
UNSAFE_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f<>&]")


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


def _wss_host(url: str) -> str:
    return url.split("?", 1)[0]


def configure_edge_tts_endpoint(url: str) -> None:
    """Point edge-tts at a synthesis WebSocket URL (module-global used by Communicate)."""
    edge_communicate.WSS_URL = url
    log(f"edge-tts WSS endpoint={_wss_host(url)}")


async def synthesize_line(text: str, voice: str, output_path: Path) -> None:
    communicate = edge_tts.Communicate(
        text,
        voice=voice,
        rate=TTS_RATE,
        pitch=TTS_PITCH,
    )
    await communicate.save(str(output_path))


def _is_tts_handshake_403(exc: BaseException) -> bool:
    if isinstance(exc, ClientResponseError) and getattr(exc, "status", None) == 403:
        return True
    text = str(exc)
    return "403" in text and ("Invalid response status" in text or "WSServerHandshakeError" in type(exc).__name__)


async def synthesize_line_with_retry(
    idx: int,
    total: int,
    speaker: str,
    content: str,
    voice: str,
    output_path: Path,
    max_attempts: int = 4,
) -> None:
    text = sanitize_tts_text(content)
    # #region agent log
    _agent_dbg(
        "TTS-A,TTS-B,TTS-C",
        "synthesize_podcast.py:line:pre",
        "about to synthesize line",
        {
            "idx": idx,
            "total": total,
            "speaker": speaker,
            "voice": voice,
            "raw_len": len(content),
            "sanitized_len": len(text),
            "changed": text != content,
            "preview": text[:80],
            "has_url": "http://" in content.lower() or "https://" in content.lower(),
            "wss_host": _wss_host(edge_communicate.WSS_URL),
        },
    )
    # #endregion
    if not text:
        raise RuntimeError(f"Line {idx} is empty after sanitization")

    last_error: Exception | None = None
    endpoint_index = 0
    for attempt in range(1, max_attempts + 1):
        try:
            await synthesize_line(text, voice, output_path)
            if attempt > 1:
                # #region agent log
                _agent_dbg(
                    "TTS-B",
                    "synthesize_podcast.py:line:retry_ok",
                    "retry succeeded",
                    {
                        "idx": idx,
                        "attempt": attempt,
                        "wss_host": _wss_host(edge_communicate.WSS_URL),
                    },
                )
                # #endregion
            return
        except NoAudioReceived as exc:
            last_error = exc
            # #region agent log
            _agent_dbg(
                "TTS-B,TTS-C,TTS-D",
                "synthesize_podcast.py:line:no_audio",
                "NoAudioReceived",
                {
                    "idx": idx,
                    "attempt": attempt,
                    "speaker": speaker,
                    "voice": voice,
                    "text_len": len(text),
                    "preview": text[:120],
                },
            )
            # #endregion
            if attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            # #region agent log
            _agent_dbg(
                "TTS-D,TTS-E",
                "synthesize_podcast.py:line:error",
                "unexpected synthesize error",
                {
                    "idx": idx,
                    "attempt": attempt,
                    "exc_type": type(exc).__name__,
                    "exc": str(exc)[:200],
                    "preview": text[:120],
                    "wss_host": _wss_host(edge_communicate.WSS_URL),
                    "is_403": _is_tts_handshake_403(exc),
                },
            )
            # #endregion
            if _is_tts_handshake_403(exc) and endpoint_index + 1 < len(TTS_WSS_URLS):
                endpoint_index += 1
                next_url = TTS_WSS_URLS[endpoint_index]
                log(
                    "edge-tts handshake 403; failing over to "
                    f"{_wss_host(next_url)}"
                )
                configure_edge_tts_endpoint(next_url)
                # #region agent log
                _agent_dbg(
                    "TTS-F",
                    "synthesize_podcast.py:line:failover",
                    "switched TTS websocket host after 403",
                    {
                        "idx": idx,
                        "attempt": attempt,
                        "wss_host": _wss_host(next_url),
                    },
                )
                # #endregion
                continue
            if attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)

    raise RuntimeError(
        f"TTS failed for line {idx}/{total} speaker={speaker} voice={voice} "
        f"text_len={len(text)} preview={text[:80]!r}"
    ) from last_error


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
    configure_edge_tts_endpoint(TTS_WSS_URLS[0])
    # #region agent log
    _agent_dbg(
        "TTS-A",
        "synthesize_podcast.py:build:start",
        "dialogue parsed",
        {
            "line_count": len(dialogue),
            "voices": SPEAKER_VOICES,
            "wss_hosts": [_wss_host(u) for u in TTS_WSS_URLS],
        },
    )
    # #endregion
    with tempfile.TemporaryDirectory(prefix="podcast-tts-") as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        for idx, (speaker, content) in enumerate(dialogue):
            voice = SPEAKER_VOICES[speaker]
            part_path = tmp_dir / f"part_{idx:04d}.mp3"
            log(f"[{idx + 1}/{len(dialogue)}] {speaker} ({voice})")
            await synthesize_line_with_retry(
                idx=idx + 1,
                total=len(dialogue),
                speaker=speaker,
                content=content,
                voice=voice,
                output_path=part_path,
            )
            parts.append(part_path)
            # Small pacing delay to reduce Edge TTS throttling.
            await asyncio.sleep(0.35)

        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        concat_with_ffmpeg(parts, output_mp3)
        log(f"Wrote {output_mp3}")
        # #region agent log
        _agent_dbg(
            "TTS-A",
            "synthesize_podcast.py:build:done",
            "podcast written",
            {"output": str(output_mp3), "parts": len(parts)},
        )
        # #endregion


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
