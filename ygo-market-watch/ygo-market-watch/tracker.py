#!/usr/bin/env python3
"""Yu-Gi-Oh! market watch, per-printing edition.

Once a day: pull every Yu-Gi-Oh! printing's TCGplayer market price from the free
tcgcsv.com mirror, save a snapshot, compare with earlier snapshots, flag possible
buyouts / climbs / crashes, send alerts, and write the data the dashboard reads.

Standard library only. Environment variables (all optional):
  GITHUB_TOKEN, GITHUB_REPOSITORY  -> open a GitHub issue for each day's alerts
  DISCORD_WEBHOOK_URL              -> also post alerts to a Discord channel
  TRACKER_DATE                     -> pretend today is this YYYY-MM-DD (testing)
"""
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data-printings"
SNAPS = DATA / "snapshots"
SITE = ROOT / "site"
SITE_DATA = SITE / "data"
BASE = "https://tcgcsv.com/tcgplayer/2"   # category 2 = Yu-Gi-Oh!
UA = "ygo-market-watch (github actions)"
HIST_CHUNKS = 256

CONFIG = json.loads((ROOT / "config.json").read_text())


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def today():
    return os.environ.get("TRACKER_DATE") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


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


def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    log(f"  failed {url}: {last}")
    return None


# ---------------------------------------------------------------- fetch
def fetch_market(date):
    """Return (prices, meta, meta_changed).

    prices: {"productId:subType": marketPrice}
    meta:   {"productId": {n: name, s: set, r: rarity, m: number, f: first-seen date}}
            sealed product / unknown ids are stored as {"x": 1, "f": ...} and ignored.
    """
    groups = get_json(f"{BASE}/groups")
    if not groups or not groups.get("results"):
        raise SystemExit("Could not load the Yu-Gi-Oh! set list from tcgcsv.com")
    groups = groups["results"]
    log(f"{len(groups)} sets found")

    meta_path = DATA / "products.json.gz"
    first_run = not meta_path.exists()
    meta = {} if first_run else read_gz(meta_path)
    min_store = CONFIG["site"]["min_store_price"]
    first_mark = "" if first_run else date   # "" = already existed when tracking began

    def do_group(g):
        gid = g["groupId"]
        pr = get_json(f"{BASE}/{gid}/prices")
        if pr is None:
            return g, None, None
        results = pr.get("results") or []
        prods = None
        if any(str(r["productId"]) not in meta for r in results):
            pd = get_json(f"{BASE}/{gid}/products")
            if pd is None:
                return g, None, None
            prods = pd.get("results") or []
        return g, results, prods

    prices, failed, meta_changed = {}, 0, first_run
    with ThreadPoolExecutor(max_workers=6) as ex:
        for g, results, prods in ex.map(do_group, groups):
            if results is None:
                failed += 1
                continue
            gname = g.get("name", "")
            for p in (prods or []):
                pid = str(p["productId"])
                if pid in meta:
                    continue
                ext = {e.get("name"): e.get("value") for e in (p.get("extendedData") or [])}
                if "Rarity" in ext or "Number" in ext:
                    meta[pid] = {"n": p.get("name", ""), "s": gname, "r": ext.get("Rarity", "") or "",
                                 "m": ext.get("Number", "") or "", "f": first_mark}
                else:
                    meta[pid] = {"x": 1, "f": first_mark}
                meta_changed = True
            for r in results:
                pid = str(r["productId"])
                if pid not in meta:            # priced but not in the product list
                    meta[pid] = {"x": 1, "f": first_mark}
                    meta_changed = True
                m = meta[pid]
                if m.get("x"):
                    continue
                try:
                    mp = float(r.get("marketPrice") or 0)
                except (TypeError, ValueError):
                    mp = 0
                if mp >= min_store:
                    prices[f'{pid}:{r.get("subTypeName") or "Normal"}'] = round(mp, 2)

    if failed > max(3, len(groups) * 0.02):
        raise SystemExit(f"{failed} of {len(groups)} sets failed to load; not saving a partial snapshot")
    if failed:
        log(f"warning: {failed} set(s) failed to load and are missing today")
    return prices, meta, meta_changed


def previous_snapshot_size(date):
    files = [f for f in sorted(SNAPS.glob("*.json.gz")) if f.name[:10] < date]
    return len(read_gz(files[-1])["prices"]) if files else None


# ---------------------------------------------------------------- analysis
def load_history(date, keep_days):
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=keep_days)).isoformat()
    snaps = []
    for f in sorted(SNAPS.glob("*.json.gz")):
        d = f.name[:10]
        if start <= d <= date:
            snaps.append((d, read_gz(f)["prices"]))
    return snaps


def price_at_or_before(snaps, target):
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
    before = snaps[:-1]
    ref7 = price_at_or_before(before, (d0 - dt.timedelta(days=7)).isoformat())
    ref30 = price_at_or_before(before, (d0 - dt.timedelta(days=30)).isoformat())
    oldest = snaps[0] if len(snaps) >= 2 else None
    ref7_any, ref30_any = ref7 or oldest, ref30 or oldest   # tables fall back to oldest snapshot
    recent = snaps[-(cfg["trend"]["window_days"] + 1):]

    # Printings that first appeared after tracking began are quiet for a while.
    qd = cfg["alerts"]["new_card_quiet_days"]
    quiet = set()
    for pid, m in meta.items():
        f = m.get("f")
        if f and (d0 - dt.date.fromisoformat(f)).days < qd:
            quiet.add(pid)

    rows, signals = [], []
    for key, p in latest.items():
        pid, sub = key.split(":", 1)
        m = meta.get(pid, {})
        p1 = prev.get(key) if prev else None
        p7 = ref7_any[1].get(key) if ref7_any else None
        p30 = ref30_any[1].get(key) if ref30_any else None
        d1, d7, d30 = pct(p, p1), pct(p, p7), pct(p, p30)
        name = m.get("n", pid)
        rows.append([key, name, m.get("s", ""), m.get("r", ""), sub, m.get("m", ""), p, d1, d7, d30])
        if pid in quiet:
            continue

        detail = " · ".join(x for x in (m.get("s"), m.get("r"), sub) if x)
        base = {"id": key, "pid": pid, "name": name, "detail": detail, "price": p}

        s = cfg["spike"]
        if p1 and d1 is not None and p >= s["min_price"] and p1 >= s["min_prev_price"] \
                and d1 >= s["min_pct"] and p - p1 >= s["min_dollar"]:
            signals.append({**base, "kind": "spike", "from": p1, "pct": d1,
                            "why": f"{money(p1)} → {money(p)} in one day (+{d1}%)"})

        t = cfg["trend"]
        if len(recent) >= 3:
            series = [r[1].get(key) for r in recent]
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

        if ref30:  # only with a real 30-day-old snapshot, no fallback
            p30r = ref30[1].get(key)
            sl = cfg["slow"]
            g30 = pct(p, p30r)
            if g30 is not None and p >= sl["min_price"] and g30 >= sl["min_pct"] and p - p30r >= sl["min_dollar"]:
                signals.append({**base, "kind": "slow", "from": p30r, "pct": g30,
                                "why": f"{money(p30r)} → {money(p)} over 30 days (+{g30}%)"})

        c = cfg["crash"]
        if p1 and d1 is not None and p1 >= c["min_prev_price"] and d1 <= -c["min_pct"] and p1 - p >= c["min_dollar"]:
            signals.append({**base, "kind": "crash", "from": p1, "pct": d1,
                            "why": f"{money(p1)} → {money(p)} in one day ({d1}%)"})

    # One signal per climbing printing: spike beats trend beats slow.
    spiked = {x["id"] for x in signals if x["kind"] == "spike"}
    trended = {x["id"] for x in signals if x["kind"] == "trend"}
    signals = [x for x in signals if not (
        (x["kind"] == "trend" and x["id"] in spiked) or
        (x["kind"] == "slow" and (x["id"] in spiked or x["id"] in trended)))]
    order = {"spike": 0, "trend": 1, "slow": 2, "crash": 3}
    signals.sort(key=lambda x: (order[x["kind"]], -abs(x["price"] - x["from"])))
    return rows, signals


def filter_new_alerts(signals, date):
    path = DATA / "alerted.json"
    seen = json.loads(path.read_text()) if path.exists() else {}
    cool = CONFIG["alerts"]["cooldown_days"]
    again = 1 + CONFIG["alerts"]["realert_extra_pct"] / 100
    d0 = dt.date.fromisoformat(date)
    fresh = []
    for s in signals:
        k = f'{s["id"]}|{s["kind"]}'
        last = seen.get(k)
        if last:
            age = (d0 - dt.date.fromisoformat(last["date"])).days
            moved_on = (s["kind"] != "crash" and s["price"] >= last["price"] * again) or \
                       (s["kind"] == "crash" and s["price"] <= last["price"] / again)
            if age < cool and not moved_on:
                continue
        fresh.append(s)
        seen[k] = {"date": date, "price": s["price"]}
    seen = {k: v for k, v in seen.items() if (d0 - dt.date.fromisoformat(v["date"])).days <= 60}
    write_json(path, seen)
    return fresh


# ---------------------------------------------------------------- site data
def write_site_data(snaps, rows, signals, fresh, date):
    site_cfg = CONFIG["site"]
    window = snaps[-site_cfg["history_days"]:]
    dates = [d for d, _ in window]

    write_json(SITE_DATA / "cards.json", {"date": date, "rows": rows})

    chunks = [dict() for _ in range(HIST_CHUNKS)]
    for key, p in snaps[-1][1].items():
        if p >= site_cfg["hist_min_price"]:
            chunks[int(key.split(":", 1)[0]) % HIST_CHUNKS][key] = [s.get(key) for _, s in window]
    for i, ch in enumerate(chunks):
        write_json(SITE_DATA / "hist" / f"{i}.json", {"dates": dates, "cards": ch})

    log_path = DATA / "alert_log.json"
    alert_log = json.loads(log_path.read_text()) if log_path.exists() else []
    alert_log = [dict(a, date=date) for a in fresh] + [a for a in alert_log if a["date"] != date]
    alert_log = alert_log[:site_cfg["alert_log_size"]]
    write_json(log_path, alert_log)

    write_json(SITE_DATA / "summary.json", {
        "date": date,
        "snapshots": len(snaps),
        "first_date": snaps[0][0],
        "priced": len(rows),
        "min_price_movers": site_cfg["min_price_movers"],
        "hist_min_price": site_cfg["hist_min_price"],
        "signals": signals,
        "alert_log": alert_log,
        "config": {k: CONFIG[k] for k in ("spike", "trend", "slow", "crash")},
    })


# ---------------------------------------------------------------- alerts
def prod_link(pid):
    return f"https://www.tcgplayer.com/product/{pid}"


LABELS = {"spike": "🚨 Possible buyout", "trend": "📈 Steady climb (7d)",
          "slow": "🐢 Slow climb (30d)", "crash": "📉 Crash"}


def send_alerts(fresh, date):
    if not fresh:
        log("no new alerts")
        return
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    site = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/" if repo else ""
    per_type = CONFIG["alerts"]["issue_rows_per_type"]

    lines = [f"**{len(fresh)} new alert(s) for {date}**", ""]
    size = 0
    for kind in ("spike", "trend", "slow", "crash"):
        group = [s for s in fresh if s["kind"] == kind]
        if not group:
            continue
        lines += [f"### {LABELS[kind]} ({len(group)})", "", "| Card | Printing | Price | Change |", "|---|---|---|---|"]
        for s in group[:per_type]:
            row = f'| [{s["name"].replace("|", "/")}]({prod_link(s["pid"])}) | {s["detail"].replace("|", "/")} | {money(s["price"])} | {s["why"]} |'
            size += len(row)
            if size > 55000:
                break
            lines.append(row)
        if len(group) > per_type or size > 55000:
            lines.append("| …more on the dashboard | | | |")
        lines.append("")
    if site:
        lines.append(f"Dashboard: {site}")
    body = "\n".join(lines)[:64000]

    if repo and token:
        n_spike = sum(1 for s in fresh if s["kind"] == "spike")
        title = f"YGO market alerts {date}: {n_spike} possible buyout(s), {len(fresh) - n_spike} other"
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues",
            data=json.dumps({"title": title, "body": body, "labels": ["alert"]}).encode(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "User-Agent": UA}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                log("opened issue", json.load(r).get("html_url"))
        except Exception as e:
            log("could not open issue:", e)

    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        msgs, cur = [], f"**YGO market alerts {date}**\n"
        for s in fresh[:80]:
            line = f'{LABELS[s["kind"]]}: **{s["name"]}** ({s["detail"]}) {money(s["price"])} — {s["why"]}\n'
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
                                         headers={"Content-Type": "application/json", "User-Agent": UA},
                                         method="POST")
            try:
                urllib.request.urlopen(req, timeout=30).close()
            except Exception as e:
                log("discord post failed:", e)


# ---------------------------------------------------------------- main
def main():
    date = today()
    prices, meta, meta_changed = fetch_market(date)

    prev_n = previous_snapshot_size(date)
    if prev_n and len(prices) < prev_n * 0.7:
        raise SystemExit(f"Only {len(prices)} priced printings today vs {prev_n} last snapshot; "
                         "not saving, the source may be incomplete")
    if not prices:
        raise SystemExit("No prices came back; not saving")

    write_gz(SNAPS / f"{date}.json.gz", {"date": date, "prices": prices})
    if meta_changed:
        write_gz(DATA / "products.json.gz", meta)
    log(f"snapshot {date}: {len(prices)} priced printings")

    snaps = load_history(date, max(CONFIG["site"]["history_days"], 35))
    rows, signals = analyze(snaps, meta, date)
    fresh = filter_new_alerts(signals, date) if len(snaps) >= 2 else []
    write_site_data(snaps, rows, signals, fresh, date)
    send_alerts(fresh, date)
    log(f"{len(signals)} signals today, {len(fresh)} new alerts, {len(snaps)} snapshots of history")


if __name__ == "__main__":
    main()
