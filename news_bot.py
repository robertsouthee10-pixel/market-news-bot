#!/usr/bin/env python3
"""
Market news bot: pulls new headlines from RSS feeds, asks Claude to rate each
one's market impact and NQ direction, and posts medium/high impact stories to
a Discord channel via webhook.

Env vars:
  ANTHROPIC_API_KEY    - Claude API key (required)
  DISCORD_WEBHOOK_URL  - Discord channel webhook (required unless DRY_RUN=1)
  DRY_RUN=1            - print posts instead of sending them
  POST_ON_FIRST_RUN=1  - on the very first run, post the backlog instead of
                         silently marking it as seen
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
STATE_FILE = ROOT / "state.json"

IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}
COLOURS = {"bullish": 0x22C55E, "bearish": 0xEF4444, "neutral": 0x9CA3AF}
ARROWS = {"bullish": "▲", "bearish": "▼", "neutral": "▬"}
BATCH_SIZE = 25
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------- state ----
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print("[warn] state.json unreadable, starting fresh")
    return {"seen": {}, "recent": []}


def save_state(state):
    cutoff = time.time() - 3 * 86400  # forget keys after 3 days
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["recent"] = state["recent"][-40:]
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- fetch ----
def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def feed_urls():
    urls = list(CONFIG.get("feeds", []))
    tpl = CONFIG.get("ticker_feed")
    if tpl:
        urls += [tpl.format(ticker=t) for t in CONFIG.get("tickers", [])]
    return urls


def fetch_articles():
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=CONFIG.get("max_age_hours", 6))
    found = {}
    for url in feed_urls():
        try:
            r = requests.get(url, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0 (market-news-bot)"})
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # one dead feed shouldn't stop the run
            print(f"[warn] feed failed: {url} ({e})")
            continue

        is_google = "news.google.com" in url
        for e in feed.entries:
            title = (e.get("title") or "").strip()
            link = e.get("link")
            if not title or not link:
                continue
            source = feed.feed.get("title", "") or ""
            if is_google:
                src = (e.get("source") or {}).get("title")
                if src:
                    source = src
                    title = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", title)

            ts = e.get("published_parsed") or e.get("updated_parsed")
            published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else now
            if now - published > max_age:
                continue

            key = hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]
            if key in found:
                continue
            summary = re.sub(r"<[^>]+>", "", e.get("summary", "") or "")
            found[key] = {
                "key": key,
                "title": title,
                "link": link,
                "source": source[:80],
                "published": published.isoformat(),
                "summary": re.sub(r"\s+", " ", summary).strip()[:400],
            }
    return list(found.values())


# ------------------------------------------------------------- classify ----
SYSTEM_PROMPT = """You are a markets news-desk analyst for a trader who trades NQ (Nasdaq-100) futures and watches the US dollar, oil, Treasury yields, VIX and these stocks: {tickers}.

For each article, judge its likely market impact over the next trading session.
- impact: "high" = can move NQ or a watched asset materially on its own (Fed decisions or Fed-chair remarks with new information, CPI/PCE/jobs surprises, megacap earnings or guidance, major geopolitical escalation hitting oil, big AI/chip export policy). "medium" = relevant and could nudge NQ or move a watched stock. "low" = routine, opinion/analysis pieces, listicles, stale news, or no clear market link.
- nq: "bullish", "bearish" or "neutral" for NQ.
- moves: up to 4 other assets clearly affected, each {{"asset": short name such as "Oil", "USD", "10Y yield", "VIX", "Gold" or a ticker, "direction": "bullish" or "bearish"}} where bullish means price up.
- reason: one plain sentence, max 25 words, explaining the call.
- duplicate: true if it is the same story as one under RECENTLY POSTED or an earlier item in this batch.

Judge only from the headline and summary given; never invent figures.
Return ONLY a JSON array with one object per article:
{{"id": <id>, "impact": "...", "nq": "...", "moves": [...], "reason": "...", "duplicate": false}}"""


def classify(articles, recent):
    from anthropic import Anthropic

    client = Anthropic()
    payload = [{"id": i, "title": a["title"], "source": a["source"], "summary": a["summary"]}
               for i, a in enumerate(articles)]
    user = ("RECENTLY POSTED:\n" + ("\n".join(recent[-30:]) or "(none)") +
            "\n\nARTICLES:\n" + json.dumps(payload, ensure_ascii=False))
    msg = client.messages.create(
        model=CONFIG.get("model", "claude-haiku-5-5"),
        max_tokens=4000,
        system=SYSTEM_PROMPT.format(tickers=", ".join(CONFIG.get("tickers", [])) or "none"),
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise ValueError(f"no JSON array in model reply: {text[:200]}")
    results = json.loads(match.group(0))
    return {r["id"]: r for r in results if isinstance(r, dict) and "id" in r}


# ---------------------------------------------------------------- post -----
def build_message(article, r):
    nq = r.get("nq") if r.get("nq") in ARROWS else "neutral"
    impact = r.get("impact", "medium")
    moves = " · ".join(
        f"{m['asset']} {ARROWS.get(m.get('direction'), '')}"
        for m in (r.get("moves") or []) if isinstance(m, dict) and m.get("asset")
    )
    fields = [
        {"name": "NQ bias", "value": f"{ARROWS[nq]} {nq.title()}", "inline": True},
        {"name": "Impact", "value": impact.title(), "inline": True},
    ]
    if moves:
        fields.append({"name": "Also moves", "value": moves[:1000], "inline": False})
    fields.append({"name": "Why", "value": (r.get("reason") or "—")[:1000], "inline": False})

    body = {"embeds": [{
        "title": article["title"][:256],
        "url": article["link"],
        "color": COLOURS[nq],
        "fields": fields,
        "footer": {"text": f"{article['source']} · AI read, not financial advice"[:2048]},
        "timestamp": article["published"],
    }]}
    if impact == "high":
        mention = CONFIG.get("high_impact_mention", "").strip()
        body["content"] = f"🚨 **HIGH IMPACT** {mention}".strip()
    return body


def post(body):
    if DRY_RUN:
        print(json.dumps(body, indent=1, ensure_ascii=False))
        return
    hook = os.environ["DISCORD_WEBHOOK_URL"]
    for _ in range(4):
        resp = requests.post(hook, json=body, timeout=15)
        if resp.status_code == 429:  # Discord rate limit
            time.sleep(float(resp.json().get("retry_after", 2)) + 0.5)
            continue
        resp.raise_for_status()
        time.sleep(1)
        return
    raise RuntimeError("Discord kept rate-limiting")


# ---------------------------------------------------------------- main -----
def send_test():
    post({"content": "✅ **Market news bot connected.** Medium and high impact "
                     "stories will appear here as they break."})
    print("Test message sent to Discord.")


def main():
    if os.environ.get("TEST_DISCORD") == "true":
        send_test()

    state = load_state()
    first_run = not state.get("initialized") and not state["seen"]
    articles = [a for a in fetch_articles() if a["key"] not in state["seen"]]
    articles.sort(key=lambda a: a["published"])
    print(f"{len(articles)} new articles")

    if first_run and os.environ.get("POST_ON_FIRST_RUN") != "1":
        for a in articles:
            state["seen"][a["key"]] = time.time()
        state["initialized"] = True
        save_state(state)
        print("First run: backlog marked as seen, nothing posted.")
        return
    state["initialized"] = True

    min_rank = IMPACT_RANK.get(CONFIG.get("min_impact", "medium"), 1)
    posted = 0
    try:
        for i in range(0, len(articles), BATCH_SIZE):
            chunk = articles[i:i + BATCH_SIZE]
            try:
                results = classify(chunk, state["recent"])
            except Exception as e:
                # leave unseen so the next run retries them
                print(f"[error] classification failed: {e}")
                continue
            for j, a in enumerate(chunk):
                r = results.get(j)
                if r:
                    print(f"  [{r.get('impact', '?'):6}] {r.get('nq', '?'):7} {a['title'][:90]}")
                if r and not r.get("duplicate") and \
                        IMPACT_RANK.get(r.get("impact"), 0) >= min_rank:
                    try:
                        post(build_message(a, r))
                    except Exception as e:
                        print(f"[error] post failed, will retry: {e}")
                        continue
                    posted += 1
                    state["recent"].append(a["title"])
                state["seen"][a["key"]] = time.time()
    finally:
        save_state(state)
    print(f"Posted {posted}")


if __name__ == "__main__":
    sys.exit(main())
