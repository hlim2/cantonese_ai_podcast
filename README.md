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
| OPENAI_BASE_URL | https://api.apifree.ai/agent/v1   |
| OPENAI_MODEL    | skywork-ai/skyclaw-v1 or ...-lite |
| OPENAI_API_KEY  | APIFree key                       |

Output: podcast_script.md with lines like 阿希: ... / 阿明: ....

**3. Synthesize audio**
scripts/synthesize_podcast.py:

parses each dialogue line
speaks them with **edge-tts** (阿希=zh-HK-HiuMaanNeural, 阿明=zh-HK-WanLungNeural)
merges with **ffmpeg** into AI_Daily_Podcast_YYYYMMDD.mp3

**4. Deliver to Telegram**
curl sendDocument uploads:

the **MP3**
the **script** (podcast_script.md)
using secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

That’s the full path: **crawled news + 編劇 prompt → SkyClaw script → Cantonese TTS → Telegram files.**
**
