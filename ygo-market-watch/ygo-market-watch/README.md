# YGO Market Watch

Tracks every Yu-Gi-Oh! card's TCGplayer price once a day and alerts you to
possible buyouts, steady climbs and crashes. Runs free on GitHub: no server, no API key.

- **Data:** the free [YGOPRODeck API](https://ygoprodeck.com/api-guide/), which includes a TCGplayer price for each card.
- **Alerts:** a GitHub issue each day something new is flagged (GitHub emails you and pushes to the GitHub mobile app), plus Discord if you add a webhook.
- **Dashboard:** a GitHub Pages site with today's flags, alert history, biggest movers (1/7/30 days), search across all cards, price charts and a watchlist.

## Setup (about 10 minutes)

1. **Create a repo.** On github.com click **New repository**, name it e.g. `ygo-market-watch`, make it **Public** (free Pages and unlimited Actions minutes need a public repo), and create it empty.
2. **Upload the files.** On the new repo page click **uploading an existing file**, drag in everything from this folder (including the `.github` folder — if your browser hides it, see the note below), and commit.
3. **Turn on Pages.** Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.
4. **Allow the job to write.** Settings → Actions → General → *Workflow permissions* → **Read and write permissions** → Save.
5. **Run it once.** Actions tab → *Daily price check* → **Run workflow**. After a minute or two, your dashboard is at `https://<your-username>.github.io/ygo-market-watch/`.
6. **Make sure you get notified.** On the repo page, click **Watch** → **All Activity**. Install the GitHub mobile app if you want phone pushes.

From then on it runs by itself every day.

> `.github` hidden? On a Mac press Cmd+Shift+. in Finder to show it. Or create the file on GitHub: **Add file → Create new file**, type `.github/workflows/daily.yml` as the name, and paste the contents.

### Optional: Discord alerts
In Discord: Server settings → Integrations → Webhooks → New webhook → Copy URL.
In GitHub: Settings → Secrets and variables → Actions → New repository secret, name `DISCORD_WEBHOOK_URL`, paste the URL.

## What to expect

- **Day 1** saves the first snapshot. Buyout alerts start **day 2**. The 7-day climb check gets meaningful after about a week, and 30-day movers after a month.
- History can't be backfilled: the free source only gives today's price, so the tracker builds its own history from day 1.

## Tuning alerts

Edit `config.json` on GitHub (pencil icon):

| Setting | Default | Meaning |
|---|---|---|
| `spike.min_pct` | 35 | One-day jump (%) that counts as a possible buyout |
| `spike.min_dollar` | 1.00 | …and it must be up at least this many dollars |
| `spike.min_price` | 2.00 | Ignore cards cheaper than this after the jump |
| `spike.min_prev_price` | 0.50 | Ignore bulk cards that were under this before |
| `trend.window_days` | 7 | Days looked at for a steady climb |
| `trend.min_pct` / `min_dollar` | 25 / 1.50 | Gain over that window |
| `trend.min_up_days` / `max_down_days` | 4 / 1 | How steady the climb must be |
| `crash.min_pct` / `min_dollar` / `min_prev_price` | 30 / 2 / 5 | One-day drop that gets flagged |
| `alerts.cooldown_days` | 5 | Don't re-alert the same card for this many days… |
| `alerts.realert_extra_pct` | 25 | …unless it has moved this much further |

Too many alerts? Raise `spike.min_price` or `spike.min_pct`. Too few? Lower them.

## Limits worth knowing

- **It sees price, not listings.** A real buyout shows up first as listings disappearing; this catches it once TCGplayer's market price reacts, so it's a little behind. TCGplayer no longer gives API keys to new developers, and scraping their site breaks their rules, so listing counts aren't available for free.
- **One price per card, not per printing.** YGOPRODeck gives one TCGplayer price per card name, not separate prices for each set or rarity. A buyout of one specific print (say a 1st Edition Starlight) may not move it.
- **Update timing** depends on how often YGOPRODeck refreshes its prices. If many days show no change, that is the source being slow, not the tracker.

## Files

- `tracker.py` — the daily job (Python standard library only)
- `config.json` — alert thresholds
- `site/index.html` — the dashboard
- `data/` — daily snapshots and alert history (written by the job; about 100 KB a day)
- `.github/workflows/daily.yml` — the schedule

Card data from YGOPRODeck. Not affiliated with TCGplayer, Konami or YGOPRODeck.
