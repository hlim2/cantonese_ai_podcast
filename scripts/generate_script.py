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
    """Safe NDJSON debug to stderr + local file. Never logs secrets."""
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
    try:
        with open("debug-840db8.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _probe_endpoint(api_key: str, base_url: str, model: str) -> dict[str, Any]:
    """Tiny probe to see whether a base URL accepts the model."""
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
                "temperature": 0,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            },
            timeout=60,
        )
        try:
            data = response.json()
        except ValueError:
            return {
                "base_url": base_url,
                "http_status": response.status_code,
                "json": False,
                "text_prefix": response.text[:120],
            }
        err = data.get("error") if isinstance(data, dict) else None
        return {
            "base_url": base_url,
            "http_status": response.status_code,
            "json": True,
            "has_choices": isinstance(data, dict) and "choices" in data,
            "body_code": data.get("code") if isinstance(data, dict) else None,
            "error_code": err.get("code") if isinstance(err, dict) else None,
            "error_message": err.get("message") if isinstance(err, dict) else None,
            "served_by": data.get("served_by") if isinstance(data, dict) else None,
        }
    except requests.RequestException as exc:
        return {"base_url": base_url, "exception": type(exc).__name__, "msg": str(exc)[:160]}


def _list_models(api_key: str, base_url: str) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/models"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError:
            return {
                "base_url": base_url,
                "http_status": response.status_code,
                "json": False,
                "text_prefix": response.text[:120],
            }
        ids: list[str] = []
        if isinstance(data, dict):
            for item in data.get("data") or []:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
        skyclaw = [mid for mid in ids if "skyclaw" in mid.lower() or "skywork" in mid.lower()]
        return {
            "base_url": base_url,
            "http_status": response.status_code,
            "json": True,
            "model_count": len(ids),
            "skyclaw_or_skywork_ids": skyclaw[:30],
            "sample_ids": ids[:20],
            "body_code": data.get("code") if isinstance(data, dict) else None,
            "error": data.get("error") if isinstance(data, dict) else None,
        }
    except requests.RequestException as exc:
        return {"base_url": base_url, "exception": type(exc).__name__, "msg": str(exc)[:160]}


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
    log(f"LLM endpoint={base_url} model={model}")
    # #region agent log
    _agent_dbg(
        "A,B",
        "generate_script.py:call_openai:pre",
        "request metadata",
        {
            "url": url,
            "base_url": base_url,
            "model": model,
            "base_has_agent": "/agent" in base_url,
            "prompt_chars": len(user_prompt),
            "key_present": bool(api_key),
            "key_length": len(api_key),
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
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API returned non-JSON (HTTP {response.status_code}): {response.text[:1000]}"
        ) from exc

    # #region agent log
    err = data.get("error") if isinstance(data, dict) else None
    _agent_dbg(
        "A,B,C",
        "generate_script.py:call_openai:post",
        "response metadata",
        {
            "http_status": response.status_code,
            "has_choices": isinstance(data, dict) and "choices" in data,
            "body_code": data.get("code") if isinstance(data, dict) else None,
            "error_code": err.get("code") if isinstance(err, dict) else None,
            "error_message": err.get("message") if isinstance(err, dict) else None,
            "served_by": data.get("served_by") if isinstance(data, dict) else None,
            "top_keys": sorted(data.keys()) if isinstance(data, dict) else [],
        },
    )
    # #endregion

    # APIFree sometimes returns HTTP 200 with an error payload.
    if isinstance(data, dict) and (
        data.get("error")
        or (isinstance(data.get("code"), int) and int(data["code"]) >= 400)
    ):
        # #region agent log
        err_msg = ""
        if isinstance(data.get("error"), dict):
            err_msg = str(data["error"].get("message") or "")
        if "model schema not found" in err_msg.lower() or data.get("code") == 500:
            candidates = [
                "https://api.apifree.ai/agent/v1",
                "https://api.apifree.ai/v1",
            ]
            # Also try alternate model id spellings against the primary base.
            model_alts = [
                model,
                "skywork-ai/skyclaw-v1",
                "skywork-ai/skyclaw-v1-lite",
                "skyclaw-v1-lite",
                "skyclaw-v1",
            ]
            probes = []
            for candidate in candidates:
                probes.append(_list_models(api_key, candidate))
                probes.append(_probe_endpoint(api_key, candidate, model))
            for alt_model in model_alts:
                if alt_model == model:
                    continue
                probes.append(_probe_endpoint(api_key, base_url, alt_model))
                probes.append(
                    _probe_endpoint(api_key, "https://api.apifree.ai/agent/v1", alt_model)
                )
            _agent_dbg(
                "A,B,D,E",
                "generate_script.py:call_openai:schema_probe",
                "probed alternate endpoints/models after schema error",
                {"probes": probes},
            )
        # #endregion
        raise RuntimeError(
            f"API error (HTTP {response.status_code}, model={model}): {data}"
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"API error {response.status_code} (model={model}): {response.text[:1000]}"
        )

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Unexpected API response (model={model}): {data}"
        ) from exc

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
        default=os.getenv("OPENAI_MODEL", "skywork-ai/skyclaw-v1-lite"),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", "https://api.apifree.ai/v1"),
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
