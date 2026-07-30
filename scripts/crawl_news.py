import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}


def log(message: str) -> None:
    print(message, file=sys.stderr)


def get_techcrunch_news(date_str: str):
    url = f"https://techcrunch.com/{date_str}/"
    log(f"Crawling TechCrunch: {url}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            log(f"TechCrunch failed with status {response.status_code}")
            return []

        pattern = rf'href="(https://techcrunch\.com/{date_str}/[^"]+)"'
        links = list(set(re.findall(pattern, response.text)))

        ai_keywords = [
            "ai",
            "intelligence",
            "model",
            "llm",
            "openai",
            "anthropic",
            "nvidia",
            "google",
            "microsoft",
            "meta",
            "claude",
            "gpt",
            "robot",
            "cyber",
            "agent",
        ]
        return [link for link in links if any(k in link.lower() for k in ai_keywords)]
    except Exception as exc:
        log(f"Error TechCrunch: {exc}")
        return []


def parse_rss(url, window_start, window_end, ai_filter=False):
    log(f"Parsing RSS: {url}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            log(f"RSS failed with status {response.status_code}")
            return []

        root = ET.fromstring(response.content)
        articles = []

        if (
            "http://www.w3.org/2005/Atom" in root.tag
            or root.find("{http://www.w3.org/2005/Atom}entry") is not None
        ):
            for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
                title = entry.find("{http://www.w3.org/2005/Atom}title").text
                link = entry.find("{http://www.w3.org/2005/Atom}link").attrib["href"]
                pub_date_str = entry.find("{http://www.w3.org/2005/Atom}published").text
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))

                if window_start <= pub_date <= window_end:
                    if not ai_filter or any(
                        k in title.lower()
                        for k in ["ai", "artificial intelligence", "model", "llm", "gpt"]
                    ):
                        articles.append(
                            {
                                "title": title,
                                "link": link,
                                "date": pub_date.isoformat(),
                            }
                        )
        else:
            for item in root.findall(".//item"):
                title = item.find("title").text
                link = item.find("link").text
                pub_date_str = item.find("pubDate").text

                formats = [
                    "%a, %d %b %Y %H:%M:%S %z",
                    "%a, %d %b %Y %H:%M:%S GMT",
                ]
                pub_date = None
                for fmt in formats:
                    try:
                        pub_date = datetime.strptime(pub_date_str, fmt)
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue

                if pub_date and window_start <= pub_date <= window_end:
                    if not ai_filter or any(
                        k in title.lower()
                        for k in ["ai", "artificial intelligence", "model", "llm", "gpt"]
                    ):
                        articles.append(
                            {
                                "title": title,
                                "link": link,
                                "date": pub_date.isoformat(),
                            }
                        )

        return articles
    except Exception as exc:
        log(f"Error RSS {url}: {exc}")
        return []


def main():
    parser = argparse.ArgumentParser(description="Crawl AI news sources")
    parser.add_argument(
        "--output",
        default="news.json",
        help="Path to write crawled news JSON",
    )
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    # HKT: [前一日] 06:00 → [今日] 05:59
    # Approximate UTC window used by the original skill.
    window_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, 22, 0, 0, tzinfo=timezone.utc
    )
    window_end = datetime(
        today.year, today.month, today.day, 5, 59, 59, tzinfo=timezone.utc
    )

    tc_date_str = yesterday.strftime("%Y/%m/%d")
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "TechCrunch": get_techcrunch_news(tc_date_str),
        "TheVerge": parse_rss(
            "https://www.theverge.com/rss/index.xml",
            window_start,
            window_end,
            ai_filter=True,
        ),
        "QbitAI": parse_rss(
            "https://www.qbitai.com/feed",
            window_start,
            window_end,
        ),
    }

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)

    log(f"Wrote {args.output}")

    # Windows self-hosted runners may use a non-UTF-8 console code page.
    output_json = json.dumps(results, indent=2, ensure_ascii=False)
    try:
        print(output_json)
    except UnicodeEncodeError:
        print(json.dumps(results, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
