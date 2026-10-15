# Market News → Discord Bot

Every 5 minutes this pulls fresh headlines (CNBC, MarketWatch, the Fed, Google News searches for Fed/macro, oil/Iran and AI chips, plus Yahoo feeds for your tickers). New stories go to Claude, which rates impact (low/medium/high), gives an NQ bias (bullish/bearish/neutral), notes other assets it moves, and explains why in one line. Medium and high stories are posted to your Discord channel; high ones get a 🚨 tag.

## Setup (about 15 minutes, do it on a computer)

**1. Discord webhook**
Server Settings → Integrations → Webhooks → New Webhook → pick the channel → Copy Webhook URL.

**2. Claude API key**
Go to console.anthropic.com, add credit, create an API key. Set a monthly spend limit while you're there.

**3. GitHub repo**
- Create a new **public** repository (public repos get unlimited free Actions minutes; a private repo would run out at this frequency). Your keys stay hidden as secrets.
- Upload all these files, keeping the `.github/workflows/news.yml` folder structure.

**4. Secrets**
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `ANTHROPIC_API_KEY` = your Claude key
- `DISCORD_WEBHOOK_URL` = your webhook URL

**5. Turn it on**
Actions tab → enable workflows → "market-news" → Run workflow.
The first run only records what's already out there (so your channel isn't flooded). Posts start from the next run onward.

## Tuning (`config.yaml`)
- `tickers` — your portfolio. Each ticker adds its own news feed.
- `min_impact` — set to `high` if it's too chatty.
- `high_impact_mention` — paste `<@&ROLE_ID>` to ping a role on high-impact news.
- `feeds` — add or remove RSS feeds. A dead feed is just skipped and logged.

## Good to know
- GitHub's scheduler can lag 5–15 minutes at busy times, so this is near-real-time, not tick-level.
- The bot commits `state.json` after runs to remember what it's already posted. That also keeps GitHub from pausing the schedule for inactivity.
- Bias calls are an AI reading of a headline, not a trade signal.

## Test locally
```
pip install -r requirements.txt
DRY_RUN=1 POST_ON_FIRST_RUN=1 ANTHROPIC_API_KEY=sk-... python news_bot.py
```
