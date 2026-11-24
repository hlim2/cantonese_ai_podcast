# cantonese_ai_podcast
End-to-end method (prompt → Telegram)
Triggered daily at **09:00 HKT** (cron: 0 1 * * * UTC) or manually via Run workflow, on GitHub-hosted ubuntu-latest.

News crawl → LLM script (prompt) → TTS MP3 → Telegram

**1. Crawl news**
scripts/crawl_news.py pulls AI-related items from TechCrunch / The Verge / QbitAI into news.json.

**2. Generate script (the “prompt” step)**
scripts/generate_script.py sends:

a fixed system prompt (粵語「AI 日報」, hosts 阿希 / 阿明, dialogue format rules)
a user prompt built from news.json
to APIFree SkyClaw:

| Setting         | Typical value                     |
|-----------------|-----------------------------------|
| OPENAI_BASE_URL | https://api.apifree.ai/v1         |
| OPENAI_MODEL    | qwen/qwen3-32b (recommended)      |
| OPENAI_API_KEY  | APIFree key                       |

**Important (from CI run 39400124359):**
- `https://api.apifree.ai/v1` + `skywork-ai/skyclaw-v1-lite` → `model schema not found`
- `https://api.apifree.ai/agent/v1` + SkyClaw → `no healthy upstream` when that upstream is down

So do **not** point Variables at `/v1` + SkyClaw. Either use `/v1` + `qwen/qwen3-32b`, or `/agent/v1` + SkyClaw when the agent upstream is healthy.

`generate_script.py` recovers in layers:

1. Retry once on gateway text like HTTP 200 `no healthy upstream`
2. Prefer general `/v1` models (`qwen/qwen3-32b`, …), then SkyClaw on `/agent/v1`. Override extras with `OPENAI_FALLBACK_MODELS`
3. If every LLM call still fails, write a deterministic offline 阿希/阿明 script from `news.json` so TTS + Telegram still run. Set `REQUIRE_LLM=true` to keep fail-hard behavior.

Output: podcast_script.md with lines like 阿希: ... / 阿明: ....

**3. Synthesize audio**
scripts/synthesize_podcast.py:

parses each dialogue line
batches them into ElevenLabs **Text-to-Dialogue** requests (≤~1800 chars each)
speaks them with **ElevenLabs** (`eleven_v4_turbo` by default)
merges chunks with **ffmpeg** into AI_Daily_Podcast_YYYYMMDD.mp3

| Setting | Typical value |
|---------|---------------|
| ELEVENLABS_API_KEY | ElevenLabs API key (secret) |
| ELEVENLABS_VOICE_AH_HEI | Voice ID for 阿希 |
| ELEVENLABS_VOICE_AH_MING | Voice ID for 阿明 |
| ELEVENLABS_MODEL | `eleven_v4_turbo` (override with `eleven_v4` for long-form quality) |

Optional: `ELEVENLABS_OUTPUT_FORMAT`, `ELEVENLABS_LANGUAGE_CODE`, `ELEVENLABS_BASE_URL`, `ELEVENLABS_MAX_CHUNK_CHARS`.

**4. Deliver to Telegram**
curl sendDocument uploads:

the **MP3**
the **script** (podcast_script.md)
using secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

TELEGRAM_CHAT_ID = Telegram group/channel ID, check ID with @IDBot
TELEGRAM_BOT_TOKEN = Bot ID, check ID with @BotFather or @IDBot

That’s the full path: **crawled news + 編劇 prompt → LLM script → ElevenLabs TTS → Telegram files.**
