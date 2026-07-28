---
name: podcast-producer
description: Automates the production of a daily AI news podcast, including news crawling, script generation, and speech synthesis. Use this skill to create daily AI news podcast episodes in Cantonese.
---

# Podcast Producer Skill

This skill automates the end-to-end production of a daily AI news podcast, specifically tailored for the "AI 日報" program. It handles news gathering from specified sources, generates a bilingual Cantonese script with two distinct speaker roles, and synthesizes the script into an MP3 audio file.

## Workflow

The skill follows these steps:

1.  **News Collection**: Automatically crawls TechCrunch for AI-related news and parses RSS feeds from The Verge and QbitAI to identify relevant articles within a defined time window.
2.  **News Filtering and Selection**: Filters and selects top AI news stories based on predefined criteria. If insufficient news is found, it can supplement with keyword-based web searches.
3.  **Script Generation**: Generates a full Cantonese podcast script (approximately 3500-5500 characters) with two hosts, "阿希" (Ah Hei) and "阿明" (Ah Ming), each with distinct speaking styles and roles. The script includes a fixed opening, structured news segments, and a closing.
4.  **Speech Synthesis**: Synthesizes the generated script into an MP3 audio file using Text-to-Speech (TTS) with specified voices for each host.

## Usage

To use this skill, follow these phases:

### Phase 1: News Crawling

Use the `scripts/crawl_news.py` script to gather news from TechCrunch, The Verge, and QbitAI. This script automatically determines the current date and the search window.

```bash
python3 /home/ubuntu/skills/podcast-producer/scripts/crawl_news.py
```

The script will output a JSON object containing the crawled news articles. This output should be saved and reviewed for the next phase.

### Phase 2: News Filtering and Script Generation

Based on the output from Phase 1, manually filter and select the most important AI news stories according to the criteria outlined in the original prompt (e.g., new model releases, major company decisions, significant product upgrades, industry breakthroughs, impact on users). If fewer than 5 articles are found, perform additional web searches using keywords like `"new AI model release [yesterday's date]"` to supplement the list.

Once the news list is finalized, generate the podcast script. The script should adhere to the following structure and length requirements:

-   **Length**: 15 to 25 minutes (approx. 3500 to 5500 Cantonese characters).
-   **Dialogue Structure**: Each news item must include at least 6-8 rounds of dialogue between "阿希" and "阿明", ensuring sufficient depth.
-   **Speaker Roles**:
    -   **阿希 (Ah Hei)**: Female, warm and enthusiastic, friendly news anchor tone. Leads the news, explains key points, and maintains a warm and approachable demeanor.
    -   **阿明 (Ah Ming)**: Male, curious and easy-going. Acts as an AI novice, asking questions, seeking clarifications, and voicing common concerns.
-   **Content Goals per News Item**:
    1.  Clearly state "what happened."
    2.  Explain unfamiliar concepts using simple analogies.
    3.  Highlight "why it matters" to AI development, daily life, or the future.
    4.  Conclude with a memorable takeaway.

Save the generated script as a Markdown file (e.g., `/home/ubuntu/podcast_script.md`).

### Phase 3: Speech Synthesis

Use the `scripts/split_script.py` script to divide the generated podcast script into smaller segments (each under 4000 characters) to comply with TTS tool limits.

```bash
SCRIPT_PATH=/home/ubuntu/podcast_script.md OUTPUT_DIR=/home/ubuntu/ MAX_CHARS=4000 python3 /home/ubuntu/skills/podcast-producer/scripts/split_script.py
```

Then, for each segment, use the `generate_speech` tool with the following speaker configurations:

-   **阿希 (Ah Hei)**: `voice_name = Sulafat`
-   **阿明 (Ah Ming)**: `voice_name = Achird`
-   **Language Code**: `zh-HK` (for Hong Kong Cantonese)

Example for a segment:

```python
print(default_api.generate_speech(
    brief="Generate speech for podcast segment X.",
    language_code="zh-HK",
    path=f"/home/ubuntu/podcast_partX.wav",
    prompt="You are a Cantonese Podcast producer. Speak in Hong Kong Cantonese.\n阿希: [female][warm and enthusiastic, friendly news anchor tone]\n阿明: [male][curious and easy-going]\n\n" + segment_content,
    speaker_voice_configs=[
        {"speaker": "阿希", "voice_name": "Sulafat"},
        {"speaker": "阿明", "voice_name": "Achird"}
    ]
))
```

After generating all `.wav` segments, concatenate them into a single MP3 file using `ffmpeg`:

```bash
ffmpeg -i /home/ubuntu/podcast_part1.wav -i /home/ubuntu/podcast_part2.wav ... -filter_complex "[0:a][1:a]...[N:a]concat=n=N:v=0:a=1[outa]" -map "[outa]" /home/ubuntu/AI_Daily_Podcast_YYYYMMDD.mp3
```

### Phase 4: Deliver Results

Deliver the final MP3 audio file and the generated Markdown script to the user.

## Bundled Resources

-   `scripts/crawl_news.py`: Python script for crawling news from TechCrunch, The Verge, and QbitAI.
-   `scripts/split_script.py`: Python script for splitting a long podcast script into smaller segments suitable for TTS.
