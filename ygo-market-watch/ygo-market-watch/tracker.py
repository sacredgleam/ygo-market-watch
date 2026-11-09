#!/usr/bin/env python3
"""Yu-Gi-Oh! market watch.

Once a day: pull every card's TCGplayer price from the free YGOPRODeck API,
save a snapshot, compare it with earlier snapshots, flag possible buyouts,
steady climbs and crashes, send alerts, and write the data the dashboard reads.

Standard library only. Run:  python tracker.py
Environment variables (all optional):
  GITHUB_TOKEN, GITHUB_REPOSITORY  -> open a GitHub issue for each day's alerts
  DISCORD_WEBHOOK_URL              -> also post alerts to a Discord channel
  FETCH_FILE                       -> read card data from a local JSON file instead of the API (testing)
  TRACKER_DATE                     -> pretend today is this YYYY-MM-DD (testing)
"""
import datetime as dt
import gzip
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPS = DATA / "snapshots"
SITE = ROOT / "site"
SITE_DATA = SITE / "data"
API_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
HIST_CHUNKS = 64

CONFIG = json.loads((ROOT / "config.json").read_text())


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def today():
    return os.environ.get("TRACKER_DATE") or dt.datetime.utcnow().strftime("%Y-%m-%d")


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_gz(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(obj, f, separators=(",", ":"))


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")


def pct(new, old):
    if old is None or new is None or old <= 0:
        return None
    return round((new - old) / old * 100, 1)


def money(x):
    return f"${x:,.2f}"


# ---------------------------------------------------------------- fetch
def fetch_cards():
    if os.environ.get("FETCH_FILE"):
        return json.loads(Path(os.environ["FETCH_FILE"]).read_text())["data"]
    req = urllib.request.Request(API_URL, headers={"User-Agent": "ygo-market-watch (github actions)"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["data"]


def take_snapshot(cards, date):
    """Save {id: price} for every card with a TCGplayer price, and refresh card names."""
    prices, meta = {}, {}
    for c in cards:
        cid = str(c["id"])
        meta[cid] = {
            "n": c.get("name", "?"),
            "t": c.get("type", ""),
            "u": c.get("ygoprodeck_url", ""),
        }
        try:
            p = float((c.get("card_prices") or [{}])[0].get("tcgplayer_price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p > 0:
            prices[cid] = round(p, 2)
    write_gz(SNAPS / f"{date}.json.gz", {"date": date, "prices": prices})
    write_gz(DATA / "cards.json.gz", meta)
    log(f"snapshot {date}: {len(prices)} priced cards of {len(cards)}")
    return meta


# ---------------------------------------------------------------- analysis
def load_history(date, keep_days):
    files = sorted(SNAPS.glob("*.json.gz"))
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=keep_days)).isoformat()
    snaps = []
    for f in files:
        d = f.name[:10]
        if start <= d <= date:
            snaps.append((d, read_gz(f)["prices"]))
    return snaps


def price_at_or_before(snaps, target):
    """Latest snapshot on or before the target date (or None)."""
    best = None
    for d, p in snaps:
        if d <= target:
            best = (d, p)
    return best


def analyze(snaps, meta, date):
    cfg = CONFIG
    latest = snaps[-1][1]
    prev = snaps[-2][1] if len(snaps) >= 2 else None
    d0 = dt.date.fromisoformat(date)
    ref7 = price_at_or_before(snaps[:-1], (d0 - dt.timedelta(days=7)).isoformat())
    ref30 = price_at_or_before(snaps[:-1], (d0 - dt.timedelta(days=30)).isoformat())
    # If history is shorter than 7/30 days, compare with the oldest snapshot we have.
    if ref7 is None and len(snaps) >= 2:
        ref7 = snaps[0]
    if ref30 is None and len(snaps) >= 2:
        ref30 = snaps[0]
    recent = snaps[-(cfg["trend"]["window_days"] + 1):]

    rows, signals = [], []
    for cid, p in latest.items():
        p1 = prev.get(cid) if prev else None
        p7 = ref7[1].get(cid) if ref7 else None
        p30 = ref30[1].get(cid) if ref30 else None
        d1, d7, d30 = pct(p, p1), pct(p, p7), pct(p, p30)
        name = meta.get(cid, {}).get("n", cid)
        rows.append([int(cid), name, p, d1, d7, d30])

        base = {"id": int(cid), "name": name, "price": p}
        # Possible buyout: sharp one-day jump.
        s = cfg["spike"]
        if p1 and d1 is not None and p >= s["min_price"] and p1 >= s["min_prev_price"] \
                and d1 >= s["min_pct"] and p - p1 >= s["min_dollar"]:
            signals.append({**base, "kind": "spike", "from": p1, "pct": d1,
                            "why": f"{money(p1)} → {money(p)} in one day (+{d1}%)"})
        # Steady climb: up over the window and up on most days.
        t = cfg["trend"]
        if len(recent) >= 3:
            series = [r[1].get(cid) for r in recent]
            series = [x for x in series if x is not None]
            if len(series) >= 3:
                first = series[0]
                ups = sum(1 for a, b in zip(series, series[1:]) if b > a)
                downs = sum(1 for a, b in zip(series, series[1:]) if b < a)
                gain = pct(p, first)
                if gain is not None and p >= t["min_price"] and gain >= t["min_pct"] \
                        and p - first >= t["min_dollar"] and ups >= t["min_up_days"] and downs <= t["max_down_days"]:
                    signals.append({**base, "kind": "trend", "from": first, "pct": gain,
                                    "why": f"{money(first)} → {money(p)} over {len(series) - 1} days, up {ups} days (+{gain}%)"})
        # Crash.
        c = cfg["crash"]
        if p1 and d1 is not None and p1 >= c["min_prev_price"] and d1 <= -c["min_pct"] and p1 - p >= c["min_dollar"]:
            signals.append({**base, "kind": "crash", "from": p1, "pct": d1,
                            "why": f"{money(p1)} → {money(p)} in one day ({d1}%)"})

    # A card that is both a spike and a trend is reported once, as a spike.
    spiked = {x["id"] for x in signals if x["kind"] == "spike"}
    signals = [x for x in signals if not (x["kind"] == "trend" and x["id"] in spiked)]
    signals.sort(key=lambda x: (x["kind"] != "spike", -(abs(x["price"] - x["from"]))))
    return rows, signals


def filter_new_alerts(signals, date):
    """Skip cards already alerted recently, unless they have climbed much further."""
    path = DATA / "alerted.json"
    seen = json.loads(path.read_text()) if path.exists() else {}
    cool = CONFIG["alerts"]["cooldown_days"]
    again = 1 + CONFIG["alerts"]["realert_extra_pct"] / 100
    d0 = dt.date.fromisoformat(date)
    fresh = []
    for s in signals:
        key = f'{s["id"]}:{s["kind"]}'
        last = seen.get(key)
        if last:
            age = (d0 - dt.date.fromisoformat(last["date"])).days
            moved_on = (s["kind"] != "crash" and s["price"] >= last["price"] * again) or \
                       (s["kind"] == "crash" and s["price"] <= last["price"] / again)
            if age < cool and not moved_on:
                continue
        fresh.append(s)
        seen[key] = {"date": date, "price": s["price"]}
    # Forget entries older than 60 days.
    seen = {k: v for k, v in seen.items() if (d0 - dt.date.fromisoformat(v["date"])).days <= 60}
    write_json(path, seen)
    return fresh


# ---------------------------------------------------------------- site data
def write_site_data(snaps, rows, signals, fresh, meta, date):
    hist_days = CONFIG["site"]["history_days"]
    window = snaps[-hist_days:]
    dates = [d for d, _ in window]

    # Every priced card, for search and the movers tables.
    write_json(SITE_DATA / "cards.json", {"date": date, "rows": rows})

    # Price history, split into chunks so a card's chart loads only a small file.
    chunks = [dict() for _ in range(HIST_CHUNKS)]
    for cid in snaps[-1][1]:
        chunks[int(cid) % HIST_CHUNKS][cid] = [p.get(cid) for _, p in window]
    for i, ch in enumerate(chunks):
        write_json(SITE_DATA / "hist" / f"{i}.json", {"dates": dates, "cards": ch})

    # Alert log (newest first), kept in the repo so the site shows past alerts too.
    log_path = DATA / "alert_log.json"
    alert_log = json.loads(log_path.read_text()) if log_path.exists() else []
    alert_log = [dict(a, date=date) for a in fresh] + [a for a in alert_log if a["date"] != date]
    alert_log = alert_log[:CONFIG["site"]["alert_log_size"]]
    write_json(log_path, alert_log)

    write_json(SITE_DATA / "summary.json", {
        "date": date,
        "snapshots": len(snaps),
        "first_date": snaps[0][0],
        "priced_cards": len(rows),
        "min_price_movers": CONFIG["site"]["min_price_movers"],
        "signals": signals,
        "alert_log": alert_log,
        "config": {k: CONFIG[k] for k in ("spike", "trend", "crash")},
    })


# ---------------------------------------------------------------- alerts
def tcg_link(name):
    return "https://www.tcgplayer.com/search/yugioh/product?q=" + urllib.parse.quote(name)


LABELS = {"spike": "🚨 Possible buyout", "trend": "📈 Steady climb", "crash": "📉 Crash"}


def send_alerts(fresh, date):
    if not fresh:
        log("no new alerts")
        return
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    site = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if repo else ""

    lines = [f"**{len(fresh)} new alert(s) for {date}**", ""]
    for kind in ("spike", "trend", "crash"):
        group = [s for s in fresh if s["kind"] == kind]
        if not group:
            continue
        lines += [f"### {LABELS[kind]} ({len(group)})", "", "| Card | Price | Change |", "|---|---|---|"]
        for s in group[:60]:
            lines.append(f'| [{s["name"]}]({tcg_link(s["name"])}) | {money(s["price"])} | {s["why"]} |')
        if len(group) > 60:
            lines.append(f"| …and {len(group) - 60} more on the dashboard | | |")
        lines.append("")
    if site:
        lines.append(f"Dashboard: {site}")
    body = "\n".join(lines)

    if repo and token:
        n_spike = sum(1 for s in fresh if s["kind"] == "spike")
        title = f"YGO market alerts {date}: {n_spike} possible buyout(s), {len(fresh) - n_spike} other"
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues",
            data=json.dumps({"title": title, "body": body, "labels": ["alert"]}).encode(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "User-Agent": "ygo-market-watch"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log("opened issue", json.load(r).get("html_url"))
        except Exception as e:  # an alert failure should not lose the day's data
            log("could not open issue:", e)

    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        msgs, cur = [], f"**YGO market alerts {date}**\n"
        for s in fresh[:80]:
            line = f'{LABELS[s["kind"]]}: **{s["name"]}** {money(s["price"])} ({s["why"]})\n'
            if len(cur) + len(line) > 1900:
                msgs.append(cur)
                cur = ""
            cur += line
        if len(fresh) > 80:
            cur += f"…and {len(fresh) - 80} more. "
        if site:
            cur += f"\n{site}"
        msgs.append(cur)
        for m in msgs:
            req = urllib.request.Request(hook, data=json.dumps({"content": m}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "ygo-market-watch"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=30).close()
            except Exception as e:
                log("discord post failed:", e)


# ---------------------------------------------------------------- main
def main():
    date = today()
    cards = fetch_cards()
    meta = take_snapshot(cards, date)
    snaps = load_history(date, max(CONFIG["site"]["history_days"], 35))
    rows, signals = analyze(snaps, meta, date)
    fresh = filter_new_alerts(signals, date) if len(snaps) >= 2 else []
    write_site_data(snaps, rows, signals, fresh, meta, date)
    send_alerts(fresh, date)
    log(f"{len(signals)} signals today, {len(fresh)} new alerts, {len(snaps)} snapshots of history")


if __name__ == "__main__":
    main()
