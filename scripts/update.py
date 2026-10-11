"""Daily updater: find new Trump 278-T filings on OGE, parse them, and rebuild
the JSON the web page reads (docs/data/).

Parsed filings are cached in docs/data/filings/<id>.json, so a run only
downloads and parses PDFs it has not seen before.
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from parse_278t import parse_pdf  # noqa: E402
from securities import Matcher, kind_of, norm_key  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
FILINGS_DIR = os.path.join(DATA, "filings")
API = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest"
UA = {"User-Agent": "Mozilla/5.0 (trump-278t-tracker)"}
PARSER_VERSION = 5


def http_get(url, timeout=120, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"  retry {url[:80]}: {e}", file=sys.stderr)
            time.sleep(2 ** (i + 1))


def list_filings():
    """Trump 278-T filings from the OGE disclosure search API."""
    q = urllib.parse.urlencode({"draw": 1, "start": 0, "length": 500, "search[value]": "Trump"})
    data = json.loads(http_get(f"{API}?{q}"))
    out = []
    for r in data["data"]:
        if not r["name"].startswith("Trump, Donald"):
            continue
        m = re.search(r"href='([^']+/([0-9A-F]{32})/\$FILE/[^']+)'>([^<]+)</a>", r["type"])
        if not m or "278 Transaction" not in m.group(3):
            continue
        url = m.group(1).replace("PAS+Index", "PAS%20Index")
        out.append({
            "id": m.group(2),
            "url": m.group(1),
            "posted": r["docDate"][:10],
            "file": urllib.parse.unquote(url.rsplit("/", 1)[1]),
            "amended": "amend" in (r.get("amended") or "").lower() or "AMENDED" in url.upper(),
        })
    out.sort(key=lambda f: (f["posted"], f["file"]))
    return out


def parse_filing(f):
    path = os.path.join(FILINGS_DIR, f"{f['id']}.json")
    if os.path.exists(path):
        cached = json.load(open(path))
        if cached.get("parser_version") == PARSER_VERSION:
            return cached
    print(f"parsing {f['posted']} {f['file']}", file=sys.stderr)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(http_get(f["url"], timeout=300))
        tmp.flush()
        rows = parse_pdf(tmp.name, int(f["posted"][:4]))
    doc = dict(f, parser_version=PARSER_VERSION, transactions=rows)
    os.makedirs(FILINGS_DIR, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    return doc


def mid(lo, hi):
    return lo if hi is None else (lo + hi) / 2


def fetch_quotes(symbols):
    """Last price and day change from Yahoo's spark endpoint (best effort)."""
    quotes = {}
    syms = sorted(set(symbols))
    for i in range(0, len(syms), 20):
        chunk = syms[i:i + 20]
        q = urllib.parse.urlencode({"symbols": ",".join(chunk), "range": "1d", "interval": "1d"})
        try:
            data = json.loads(http_get(f"https://query2.finance.yahoo.com/v7/finance/spark?{q}",
                                       timeout=30, tries=2))
        except Exception as e:  # noqa: BLE001
            print(f"  quotes failed for {chunk[0]}..: {e}", file=sys.stderr)
            continue
        for res in data.get("spark", {}).get("result", []):
            try:
                meta = res["response"][0]["meta"]
                quotes[res["symbol"]] = {
                    "price": meta["regularMarketPrice"],
                    "prev": meta.get("chartPreviousClose") or meta.get("previousClose"),
                }
            except (KeyError, IndexError, TypeError):
                pass
        time.sleep(0.3)
    return quotes


def build(filings, docs, matcher):
    latest_posted = max(f["posted"] for f in filings)
    latest_ids = {f["id"] for f in filings if f["posted"] == latest_posted}

    secs = {}
    filing_summ = []
    latest_tx = []
    for f in filings:
        d = docs[f["id"]]
        s = {k: f[k] for k in ("id", "url", "posted", "file", "amended")}
        s.update(n=0, n_buy=0, n_sell=0, buy=0.0, sell=0.0, date_min=None, date_max=None,
                 by_kind=defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "n": 0}))
        for t in d["transactions"]:
            kind = kind_of(t["description"])
            ticker = matcher.match(t["description"]) if kind in ("stock", "etf") else None
            key = ticker or f"{kind}:{norm_key(t['description'])[:40]}"
            m = mid(t["amount_low"], t["amount_high"])
            buy = t["type"] == "purchase"
            s["n"] += 1
            s["n_buy" if buy else "n_sell"] += 1
            s["buy" if buy else "sell"] += m if t["type"] != "exchange" else 0
            bk = s["by_kind"][kind]
            bk["buy" if buy else "sell"] += m
            bk["n"] += 1
            if t["date"]:
                s.setdefault("_dates", []).append(t["date"])

            sec = secs.setdefault(key, {"key": key, "ticker": ticker, "kind": kind,
                                        "names": defaultdict(int), "by_filing": {},
                                        "last_date": None})
            sec["names"][t["description"]] += 1
            bf = sec["by_filing"].setdefault(f["id"], {"buy": 0.0, "sell": 0.0, "nb": 0, "ns": 0})
            if buy:
                bf["buy"] += m
                bf["nb"] += 1
            else:
                bf["sell"] += m
                bf["ns"] += 1
            if t["date"] and (not sec["last_date"] or t["date"] > sec["last_date"]):
                sec["last_date"] = t["date"]
            if f["id"] in latest_ids:
                latest_tx.append(dict(t, kind=kind, ticker=ticker, filing=f["id"]))
        s["by_kind"] = dict(s["by_kind"])
        # OCR-garbled dates scatter outliers; report the 3rd-97th percentile span.
        ds = sorted(s.pop("_dates", []))
        if ds:
            s["date_min"], s["date_max"] = ds[int(len(ds) * 0.03)], ds[min(len(ds) - 1, int(len(ds) * 0.97))]
        filing_summ.append(s)

    tickers = [s["ticker"] for s in secs.values() if s["ticker"]]
    quotes = fetch_quotes(tickers + ["KRW=X"])

    sec_list = []
    for s in secs.values():
        name = max(s["names"].items(), key=lambda kv: kv[1])[0]
        tot_buy = sum(b["buy"] for b in s["by_filing"].values())
        tot_sell = sum(b["sell"] for b in s["by_filing"].values())
        lat = [s["by_filing"][i] for i in latest_ids if i in s["by_filing"]]
        out = {
            "key": s["key"], "ticker": s["ticker"], "kind": s["kind"], "name": name,
            "buy": round(tot_buy), "sell": round(tot_sell), "net": round(tot_buy - tot_sell),
            "n_buy": sum(b["nb"] for b in s["by_filing"].values()),
            "n_sell": sum(b["ns"] for b in s["by_filing"].values()),
            "latest_buy": round(sum(b["buy"] for b in lat)),
            "latest_sell": round(sum(b["sell"] for b in lat)),
            "first_filing": min((f["posted"] for f in filings if f["id"] in s["by_filing"])),
            "last_date": s["last_date"],
            "by_filing": {k: [round(v["buy"]), round(v["sell"])] for k, v in s["by_filing"].items()},
        }
        out["latest_net"] = out["latest_buy"] - out["latest_sell"]
        q = quotes.get(s["ticker"]) if s["ticker"] else None
        if q:
            out["price"] = q["price"]
            if q.get("prev"):
                out["chg_pct"] = round((q["price"] / q["prev"] - 1) * 100, 2)
        sec_list.append(out)

    # Equities/ETFs carry the page; bonds and cash are summarised to keep the file small.
    keep = [s for s in sec_list if s["kind"] in ("stock", "etf")]
    other = [s for s in sec_list if s["kind"] not in ("stock", "etf")]
    other.sort(key=lambda s: -(s["latest_buy"] + s["latest_sell"] + abs(s["net"]) / 1000))
    for s in other[200:]:
        s.pop("by_filing", None)
    keep.sort(key=lambda s: -s["net"])

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "latest_posted": latest_posted,
        "latest_ids": sorted(latest_ids),
        "usdkrw": (quotes.get("KRW=X") or {}).get("price"),
        "filings": filing_summ,
        "securities": keep,
        "other": other[:200],
        "other_totals": {
            k: {"buy": round(sum(s["buy"] for s in other if s["kind"] == k)),
                "sell": round(sum(s["sell"] for s in other if s["kind"] == k)),
                "count": sum(1 for s in other if s["kind"] == k)}
            for k in ("bond", "cash")
        },
        "latest_transactions": sorted(latest_tx, key=lambda t: (-(t["amount_low"]), t["description"])),
    }


PICK_THRESHOLD = 0.05  # only follow buys that are at least 5% of a filing's net buying


def build_plan(summary):
    """Turn each disclosure date into concrete orders for someone mirroring it.

    Buys: tickers whose net buying is >= PICK_THRESHOLD of that date's total net
    buying, re-weighted to sum to 100%. Sells: tickers the follower holds (from an
    earlier buy order) that Trump net-sold, sized as the share of his position
    he sold."""
    secs = {s["ticker"]: s for s in summary["securities"] if s["ticker"]}
    by_date = defaultdict(list)
    period = defaultdict(list)
    for f in summary["filings"]:
        by_date[f["posted"]].append(f["id"])
        period[f["posted"]] += [d for d in (f["date_min"], f["date_max"]) if d]

    trump_pos = defaultdict(float)  # Trump's estimated net position per ticker
    held = {}  # ticker -> date of the buy order that put it in the follower's portfolio
    actions = []
    for date in sorted(by_date):
        ids = by_date[date]
        net = {}
        for t, s in secs.items():
            v = sum(s["by_filing"][i][0] - s["by_filing"][i][1] for i in ids if i in s["by_filing"])
            if v:
                net[t] = v
        pos_total = sum(v for v in net.values() if v > 0)
        picks = sorted(((t, v) for t, v in net.items() if v > 0 and v >= PICK_THRESHOLD * pos_total),
                       key=lambda kv: -kv[1])
        pick_total = sum(v for _, v in picks)
        sells = []
        for t, v in sorted(net.items(), key=lambda kv: kv[1]):
            if v >= 0 or t not in held:
                continue
            before = max(trump_pos[t], 0)
            frac = 1.0 if before <= 0 else min(1.0, -v / before)
            sells.append({"ticker": t, "name": secs[t]["name"], "trump_net": round(v),
                          "fraction": round(frac, 3), "full": frac >= 0.999 or before + v <= 0,
                          "bought_on": held[t]})
        for t, v in net.items():
            trump_pos[t] += v
        for s in sells:
            if s["full"]:
                held.pop(s["ticker"], None)
        buys = [{"ticker": t, "name": secs[t]["name"], "kind": secs[t]["kind"], "trump_net": round(v),
                 "weight": round(v / pick_total, 4), "also_held": t in held}
                for t, v in picks]
        for b in buys:
            held.setdefault(b["ticker"], date)
        p = sorted(period[date])
        actions.append({"date": date, "period": [p[0], p[-1]] if p else None,
                        "buys": buys, "sells": sells})
    holding = [{"ticker": t, "name": secs[t]["name"], "since": d, "trump_pos": round(trump_pos[t]),
                "price": secs[t].get("price"), "chg_pct": secs[t].get("chg_pct")}
               for t, d in sorted(held.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)]
    return {"threshold": PICK_THRESHOLD, "actions": actions[::-1], "holding": holding}


def write_alert(summary, new, path):
    """Markdown body for the new-filing notification (GitHub issue / push)."""
    a = summary["plan"]["actions"][0]
    secs = {s["ticker"]: s for s in summary["securities"] if s["ticker"]}
    lines = [f"트럼프 거래 신고서(278-T)가 새로 공개됐습니다: {len(new)}건, 공개일 {a['date']}.", ""]
    if a.get("period"):
        lines += [f"트럼프의 실제 거래일: {a['period'][0]} ~ {a['period'][1]}", ""]
    lines += [f"**매수 ({len(a['buys'])}종목, 비중 {round(summary['plan']['threshold'] * 100)}% 이상)**", ""]
    for b in a["buys"]:
        price = secs.get(b["ticker"], {}).get("price")
        lines.append(f"- {b['ticker']} {b['weight'] * 100:.1f}% — {b['name']}" + (f" (${price:.2f})" if price else ""))
    # Sells depend on when each follower started, so list everything Trump net-sold
    # among tickers that were ever a buy order; the page sizes it per follower.
    ever = {b["ticker"] for act in summary["plan"]["actions"] for b in act["buys"]}
    sold = [(t, s) for t, s in secs.items() if t in ever and s["latest_net"] < 0]
    sold.sort(key=lambda kv: kv[1]["latest_net"])
    lines += ["", "**트럼프가 판 종목 (갖고 있다면 페이지에서 매도 비율 확인)**", ""]
    lines += [f"- {t} — {s['name']}" for t, s in sold] or ["- 없음"]
    lines += ["", "주문 수량과 매도 비율: https://shinhyuk.github.io/trump/",
              "", "공시 원문:"] + [f"- {f['posted']} {f['file']}: {f['url']}" for f in new]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    os.makedirs(FILINGS_DIR, exist_ok=True)
    state_path = os.path.join(DATA, "state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    filings = list_filings()
    print(f"{len(filings)} Trump 278-T filings on OGE", file=sys.stderr)
    known = set(state.get("known_ids", []))
    new = [f for f in filings if f["id"] not in known]
    docs = {f["id"]: parse_filing(f) for f in filings}
    matcher = Matcher.download()
    summary = build(filings, docs, matcher)
    summary["plan"] = build_plan(summary)
    summary["new_since_last_run"] = [f["id"] for f in new] if known else []
    now = summary["generated_at"]
    state = {"known_ids": [f["id"] for f in filings], "last_checked": now,
             "last_new_filing": (now if new and known else state.get("last_new_filing")) or now}
    summary["last_checked"] = now
    summary["last_new_filing_detected"] = state["last_new_filing"]
    with open(os.path.join(DATA, "summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, separators=(",", ":"))
    with open(state_path, "w") as fh:
        json.dump(state, fh, indent=1)
    alert_path = os.environ.get("ALERT_PATH")
    if alert_path and new and known:
        write_alert(summary, new, alert_path)
        print(f"alert written to {alert_path}", file=sys.stderr)
    print(f"new filings: {len(new)}; securities: {len(summary['securities'])}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--alert-only":
        # Notification test: render the alert for the latest disclosure as if it were new.
        summ = json.load(open(os.path.join(DATA, "summary.json")))
        latest = [f for f in summ["filings"] if f["id"] in summ["latest_ids"]]
        write_alert(summ, latest, sys.argv[2])
    else:
        main()
