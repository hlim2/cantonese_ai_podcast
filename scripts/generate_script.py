#!/usr/bin/env python3
"""Generate a Cantonese podcast script from crawled news via OpenAI-compatible API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

SYSTEM_PROMPT = """你係「AI 日報」粵語 Podcast 編劇。
請用香港粵語口語（書面可以夾雜粵語詞），寫一段雙主持對話稿。

主持角色：
- 阿希：女主持，溫暖熱情，新聞主播語氣，負責帶新聞同解釋重點
- 阿明：男主持，好奇隨和，扮演 AI 新手，負責發問同講出聽眾疑慮

硬性要求：
1. 只輸出對話稿本身，不要前言、不要 Markdown 標題、不要 JSON
2. 每一句必須用下面格式其中一種開頭：
   阿希: ...
   阿明: ...
3. 先寫開場，再講 3 至 5 則最重要 AI 新聞，最後結尾
4. 每則新聞至少 6 輪對話（阿希/阿明來回）
5. 每則新聞要清楚講：發生咗咩、簡單比喻解釋概念、點解重要、一句 takeaway
6. 總字數大約 1800 至 3500 字（GitHub Actions / TTS 可負擔長度）
7. 如果新聞不足，可以合併相關新聞，但唔好虛構具體公司數據
"""


def log(message: str) -> None:
    print(message, file=sys.stderr)


# #region agent log
def _agent_dbg(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    """Emit safe debug NDJSON to stderr + optional local log file. Never logs secrets."""
    import time

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
    for candidate in (
        Path("debug-840db8.log"),
        Path(__file__).resolve().parents[2] / "debug-840db8.log",
        Path("/home/runner/work/cantonese_ai_podcast/cantonese_ai_podcast/debug-840db8.log"),
    ):
        try:
            with candidate.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            break
        except OSError:
            continue


# #endregion


def flatten_news(payload: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    for link in payload.get("TechCrunch") or []:
        items.append({"source": "TechCrunch", "title": link, "link": link})

    for article in payload.get("TheVerge") or []:
        items.append(
            {
                "source": "The Verge",
                "title": article.get("title") or "",
                "link": article.get("link") or "",
            }
        )

    for article in payload.get("QbitAI") or []:
        items.append(
            {
                "source": "QbitAI",
                "title": article.get("title") or "",
                "link": article.get("link") or "",
            }
        )

    # De-duplicate by link/title while preserving order.
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in items:
        key = (item.get("link") or item.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def build_user_prompt(news_items: list[dict[str, str]], date_label: str) -> str:
    if not news_items:
        news_block = "（今日未能抓到足夠新聞，請做一集簡短 AI 日報開場+市場觀察+結尾，仍用阿希/阿明格式。）"
    else:
        lines = []
        for idx, item in enumerate(news_items[:12], start=1):
            lines.append(
                f"{idx}. [{item['source']}] {item['title']}\n   URL: {item['link']}"
            )
        news_block = "\n".join(lines)

    return (
        f"日期：{date_label}\n"
        "請根據以下新聞素材撰寫今日粵語 Podcast 對話稿：\n\n"
        f"{news_block}\n"
    )


def call_openai(api_key: str, model: str, base_url: str, user_prompt: str) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    # #region agent log
    key_stripped = api_key.strip()
    key_kind = (
        "openai_sk"
        if key_stripped.startswith("sk-")
        else "empty"
        if not key_stripped
        else "other"
    )
    _agent_dbg(
        "A,B,C",
        "generate_script.py:call_openai:pre",
        "auth request metadata",
        {
            "url": url,
            "base_url": base_url,
            "model": model,
            "key_present": bool(key_stripped),
            "key_length": len(key_stripped),
            "key_kind": key_kind,
            "key_has_whitespace": api_key != key_stripped,
            "key_has_quotes": key_stripped[:1] in "'\"" or key_stripped[-1:] in "'\"",
        },
    )
    # #endregion
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=180,
    )
    # #region agent log
    try:
        preview = response.json()
        resp_keys = sorted(preview.keys()) if isinstance(preview, dict) else []
        body_code = preview.get("code") if isinstance(preview, dict) else None
        err = preview.get("error") if isinstance(preview, dict) else None
        err_code = err.get("code") if isinstance(err, dict) else None
        err_type = err.get("type") if isinstance(err, dict) else None
        served_by = preview.get("served_by") if isinstance(preview, dict) else None
        has_choices = isinstance(preview, dict) and "choices" in preview
    except Exception as parse_exc:  # noqa: BLE001
        resp_keys = []
        body_code = None
        err_code = None
        err_type = None
        served_by = None
        has_choices = False
        preview = {"_parse_error": str(parse_exc), "_text_prefix": response.text[:200]}
    _agent_dbg(
        "D,E",
        "generate_script.py:call_openai:post",
        "auth response metadata",
        {
            "http_status": response.status_code,
            "resp_keys": resp_keys,
            "body_code": body_code,
            "error_code": err_code,
            "error_type": err_type,
            "served_by": served_by,
            "has_choices": has_choices,
        },
    )
    # #endregion
    if response.status_code >= 400:
        raise RuntimeError(
            f"OpenAI API error {response.status_code}: {response.text[:1000]}"
        )

    data = response.json() if not isinstance(preview, dict) or "_parse_error" in preview else preview
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected API response: {data}") from exc

    if not content or not str(content).strip():
        raise RuntimeError("API returned empty script content")
    return str(content).strip()


def normalize_script(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3]
        cleaned = cleaned.strip()
    return cleaned + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Cantonese podcast script")
    parser.add_argument("--news", default="news.json")
    parser.add_argument("--output", default="podcast_script.md")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required")

    news_path = Path(args.news)
    if not news_path.exists():
        raise SystemExit(f"News file not found: {news_path}")

    payload = json.loads(news_path.read_text(encoding="utf-8"))
    news_items = flatten_news(payload)
    date_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"Using {len(news_items)} news items for script generation")

    script = call_openai(
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
        user_prompt=build_user_prompt(news_items, date_label),
    )
    script = normalize_script(script)

    output_path = Path(args.output)
    output_path.write_text(script, encoding="utf-8")
    log(f"Wrote {output_path} ({len(script)} chars)")


if __name__ == "__main__":
    main()
