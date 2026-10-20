#!/usr/bin/env python3
"""Generate a Cantonese podcast script from crawled news via OpenAI-compatible API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Transient gateway / proxy failures that should trigger retry + alternate configs.
_GATEWAY_ERROR_MARKERS = (
    "no healthy upstream",
    "upstream connect error",
    "connection refused",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "503 service",
    "502 bad",
    "504 gateway",
)

SYSTEM_PROMPT = """你係「AI 日報」粵語 Podcast 編劇。
請用香港粵語口語（書面可以夾雜粵語詞），寫一段雙主持對話稿。

語氣要求（好重要）：
- 語氣自然、親切、似真人 Podcast
- 避免太書面、太新聞稿、太機械式
- 多用短句、口語反應（例如：係呀、吓、真係、即係、咁呀）
- 對話要有來有往，像兩個人傾偈，唔好似輪流讀稿

主持角色：
- 阿希：女主持，溫暖親切、帶住輕鬆主持感（唔好太正襟危坐新聞腔），負責帶新聞同用淺白解釋重點
- 阿明：男主持，好奇隨和，扮演 AI 新手，負責發問、插咀、講出聽眾疑慮

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
                # Required by APIFree for Qwen3; harmless for other chat models.
                "enable_thinking": False,
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


def _looks_like_gateway_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _GATEWAY_ERROR_MARKERS)


def _is_recoverable_apifree_error(data: dict[str, Any]) -> bool:
    """True when retrying another apifree model/base URL may succeed."""
    if not isinstance(data, dict):
        return False
    if data.get("_non_json") or data.get("_transport_error"):
        return True
    if isinstance(data.get("code"), int) and int(data["code"]) >= 400:
        return True
    err = data.get("error")
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").lower()
    msg = str(err.get("message") or "").lower()
    if code in (
        "model_not_found",
        "model_schema_not_found",
        "upstream_error",
        "server_error",
        "timeout",
    ):
        return True
    if "model schema not found" in msg or "model not exist" in msg:
        return True
    if _looks_like_gateway_error(msg):
        return True
    return False


def _apifree_fallback_configs(model: str, base_url: str) -> list[tuple[str, str]]:
    """Ordered (base_url, model) pairs to try after the primary config fails."""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []

    def add(url: str, mid: str) -> None:
        key = (url.rstrip("/"), mid)
        if key in seen:
            return
        seen.add(key)
        ordered.append(key)

    add(base_url, model)
    if "apifree.ai" not in base_url.lower():
        return ordered
    for url in (
        "https://api.apifree.ai/agent/v1",
        "https://api.apifree.ai/v1",
    ):
        for mid in (
            "skywork-ai/skyclaw-v1-lite",
            "skywork-ai/skyclaw-v1",
            "skyclaw-v1-lite",
            "skyclaw-v1",
        ):
            add(url, mid)
    return ordered


def _chat_completion(
    api_key: str,
    model: str,
    base_url: str,
    user_prompt: str,
) -> tuple[requests.Response | None, dict[str, Any]]:
    """POST chat/completions. Gateway/non-JSON failures return a synthetic error dict."""
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
                # Required by APIFree for Qwen3; harmless for other chat models.
                "enable_thinking": False,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=180,
        )
    except requests.RequestException as exc:
        return None, {
            "_transport_error": True,
            "code": 503,
            "error": {
                "code": "upstream_error",
                "message": f"{type(exc).__name__}: {exc}"[:500],
            },
        }

    try:
        data = response.json()
    except ValueError:
        body = (response.text or "").strip()
        return response, {
            "_non_json": True,
            "code": response.status_code if response.status_code >= 400 else 503,
            "error": {
                "code": "upstream_error",
                "message": body[:500] or f"empty non-JSON body (HTTP {response.status_code})",
            },
            "raw_prefix": body[:120],
        }
    if not isinstance(data, dict):
        return response, {
            "_non_json": True,
            "code": 502,
            "error": {
                "code": "upstream_error",
                "message": f"Unexpected API response type: {type(data).__name__}",
            },
        }
    return response, data


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
    last_error: dict[str, Any] | None = None
    last_status = 0
    tried: list[dict[str, str]] = []
    # Per-config transient retries for gateway/upstream blips before switching config.
    max_transient_retries = 2
    # Skip other models on a base URL that already returned a gateway/upstream failure.
    dead_base_urls: set[str] = set()

    for attempt_base, attempt_model in _apifree_fallback_configs(model, base_url):
        normalized_base = attempt_base.rstrip("/")
        if normalized_base in dead_base_urls:
            continue

        url = normalized_base + "/chat/completions"
        is_primary = normalized_base == base_url.rstrip("/") and attempt_model == model
        if is_primary:
            log(f"LLM endpoint={attempt_base} model={attempt_model}")
        else:
            log(
                f"Retrying LLM endpoint={attempt_base} model={attempt_model} "
                f"(primary was {base_url} / {model})"
            )
        tried.append({"base_url": attempt_base, "model": attempt_model})

        move_to_next_config = False
        for transient_idx in range(max_transient_retries):
            # #region agent log
            _agent_dbg(
                "A,B",
                "generate_script.py:call_openai:pre",
                "request metadata",
                {
                    "url": url,
                    "base_url": attempt_base,
                    "model": attempt_model,
                    "base_has_agent": "/agent" in attempt_base,
                    "prompt_chars": len(user_prompt),
                    "key_present": bool(api_key),
                    "key_length": len(api_key),
                    "is_primary": is_primary,
                    "transient_attempt": transient_idx + 1,
                },
            )
            # #endregion

            response, data = _chat_completion(
                api_key, attempt_model, attempt_base, user_prompt
            )
            http_status = (
                response.status_code
                if response is not None
                else int(data.get("code") or 503)
            )

            # #region agent log
            err = data.get("error")
            _agent_dbg(
                "A,B,C",
                "generate_script.py:call_openai:post",
                "response metadata",
                {
                    "http_status": http_status,
                    "has_choices": "choices" in data,
                    "body_code": data.get("code"),
                    "error_code": err.get("code") if isinstance(err, dict) else None,
                    "error_message": (
                        err.get("message") if isinstance(err, dict) else None
                    ),
                    "served_by": data.get("served_by"),
                    "top_keys": sorted(data.keys()),
                    "model": attempt_model,
                    "base_url": attempt_base,
                    "non_json": bool(data.get("_non_json")),
                    "transport_error": bool(data.get("_transport_error")),
                    "transient_attempt": transient_idx + 1,
                },
            )
            # #endregion

            if http_status >= 400 or data.get("_non_json") or data.get("_transport_error"):
                last_error = data
                last_status = http_status
                err_msg = ""
                if isinstance(err, dict):
                    err_msg = str(err.get("message") or "")
                is_gateway = bool(
                    data.get("_non_json")
                    or data.get("_transport_error")
                    or _looks_like_gateway_error(err_msg)
                    or http_status in (502, 503, 504)
                )
                if is_gateway and transient_idx + 1 < max_transient_retries:
                    sleep_s = 2 ** transient_idx
                    log(
                        f"Transient upstream/gateway error "
                        f"(HTTP {http_status}): "
                        f"{err_msg[:120] or data.get('raw_prefix', '')!r}; "
                        f"sleeping {sleep_s}s then retrying same config"
                    )
                    # #region agent log
                    _agent_dbg(
                        "C",
                        "generate_script.py:call_openai:transient_retry",
                        "sleeping before same-config retry",
                        {
                            "sleep_s": sleep_s,
                            "http_status": http_status,
                            "err_msg": err_msg[:160],
                            "base_url": attempt_base,
                            "model": attempt_model,
                        },
                    )
                    # #endregion
                    time.sleep(sleep_s)
                    continue
                if is_gateway:
                    dead_base_urls.add(normalized_base)
                    log(
                        f"Marking base URL unhealthy, skipping sibling models: "
                        f"{normalized_base}"
                    )
                    # #region agent log
                    _agent_dbg(
                        "B",
                        "generate_script.py:call_openai:dead_base",
                        "skipping remaining models on unhealthy base URL",
                        {
                            "base_url": normalized_base,
                            "err_msg": err_msg[:160],
                            "http_status": http_status,
                        },
                    )
                    # #endregion
                move_to_next_config = True
                break

            if data.get("error") or (
                isinstance(data.get("code"), int) and int(data["code"]) >= 400
            ):
                last_error = data
                last_status = http_status
                if _is_recoverable_apifree_error(data):
                    move_to_next_config = True
                    break
                raise RuntimeError(
                    f"API error (HTTP {last_status}, model={attempt_model}, "
                    f"base_url={attempt_base}): {last_error!r}"
                )

            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                last_error = data
                last_status = http_status
                if _is_recoverable_apifree_error(data):
                    move_to_next_config = True
                    break
                raise RuntimeError(
                    f"Unexpected API response (model={attempt_model}): {data}"
                ) from exc

            if not content or not str(content).strip():
                last_error = {"error": {"message": "empty content"}}
                last_status = http_status
                move_to_next_config = True
                break

            if not is_primary or transient_idx > 0:
                log(
                    f"Recovered using LLM config: {attempt_base} / {attempt_model} "
                    f"(transient_attempt={transient_idx + 1})"
                )
            return str(content).strip()

        if move_to_next_config:
            continue

    # #region agent log
    err_msg = ""
    if isinstance(last_error, dict) and isinstance(last_error.get("error"), dict):
        err_msg = str(last_error["error"].get("message") or "")
    if _is_recoverable_apifree_error(last_error or {}):
        candidates = [
            "https://api.apifree.ai/agent/v1",
            "https://api.apifree.ai/v1",
        ]
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
            "probed alternate endpoints/models after exhausting fallbacks",
            {"probes": probes, "tried": tried, "last_err_msg": err_msg},
        )
    # #endregion

    raise RuntimeError(
        f"API error (HTTP {last_status}, model={model}, base_url={base_url}): "
        f"{last_error!r}. Tried configs: {tried}"
    )


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
        default=os.getenv("OPENAI_BASE_URL", "https://api.apifree.ai/agent/v1"),
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
