#!/usr/bin/env python3
"""Generate a Cantonese podcast script via an OpenAI-compatible API (Ollama by default)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen2.5:7b"

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
    try:
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
            timeout=600,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            "Cannot reach Ollama at "
            f"{base_url}. Is Docker Desktop running and is the Ollama "
            "container publishing port 11434? "
            "Try: curl http://127.0.0.1:11434/api/tags"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(
            f"Timed out waiting for model response from {base_url} "
            f"(model={model}). Try a smaller model or increase resources."
        ) from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"LLM API error {response.status_code}: {response.text[:1000]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"LLM API returned non-JSON response: {response.text[:500]}"
        ) from exc

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"LLM API error payload: {data}")

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
        default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
    )
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY", "").strip() or "ollama"

    news_path = Path(args.news)
    if not news_path.exists():
        raise SystemExit(f"News file not found: {news_path}")

    payload = json.loads(news_path.read_text(encoding="utf-8"))
    news_items = flatten_news(payload)
    date_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"Using {len(news_items)} news items for script generation")
    log(f"LLM endpoint={args.base_url} model={args.model}")

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
