"""PEFINDO rating-change monitor.

Polls PEFINDO's public rating-action feed, detects new rating changes
(upgrade, downgrade, outlook change, credit watch, withdrawal), and alerts via
ntfy push and Gmail. Also maintains docs/data.json for the dashboard.

Stdlib only, so it runs on a bare GitHub Actions runner.

Usage:
  python checker.py                 normal run (poll, alert, save state)
  python checker.py --dry-run       poll and print alerts, send nothing, save nothing
  python checker.py --resync        rebuild docs/data.json from the full history
  python checker.py --test TICKER   send a test alert for TICKER's latest action

Config (environment variables / GitHub secrets):
  NTFY_TOPIC    ntfy topic name (keep it unguessable)
  SMTP_USER     Gmail address used to send
  SMTP_PASS     Gmail app password
  MAIL_TO       recipient(s), comma separated
  WATCHLIST     JSON: {"tickers": {"ABCD": "Bond", ...}, "keywords": {"some bank": "Deposit", ...}}
"""

import argparse
import base64
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE = "https://www.pefindo.com/"
FEED_URL = BASE + "rating-action-reports/get-rating-action"
COMPANY_URL = BASE + "rating-action-reports/rating-report/{}"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")
UA = "Mozilla/5.0 (rating-change monitor; low-frequency polling)"

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"
LOCAL_WATCHLIST = ROOT / "watchlist.json"  # gitignored; used when WATCHLIST env is absent

POLL_ROWS = 200          # rows fetched per normal run (feed is newest-first)
SEEN_KEEP = 3000         # dedupe keys retained in state
WIB = timezone(timedelta(hours=7))

# Long-term national scale, best to worst, for notch counting.
SCALE = ["idAAA", "idAA+", "idAA", "idAA-", "idA+", "idA", "idA-",
         "idBBB+", "idBBB", "idBBB-", "idBB+", "idBB", "idBB-",
         "idB+", "idB", "idB-", "idCCC", "idCC", "idC", "idSD", "idD"]
INVESTMENT_GRADE_FLOOR = SCALE.index("idBBB-")
RATING_RE = r"id(?:AAA|AA|A|BBB|BB|B|CCC|CC|C|SD|D)[+-]?"
MOVE_RE = re.compile(
    rf"to\s+[\"'“‘]?({RATING_RE})(?:\s*\(sy\))?[\"'”’]?\s*,?\s+from\s+"
    rf"(?:(?:its\s+|the\s+)?previous(?:ly)?\s+)?[\"'“‘]?({RATING_RE})",
    re.I)


# ---------------------------------------------------------------- fetching

def http(url, data=None, retries=3):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = {"User-Agent": UA}
    if data is not None:
        headers["X-Requested-With"] = "XMLHttpRequest"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def fetch_feed(length):
    raw = http(FEED_URL, {"draw": 1, "start": 0, "length": length,
                          "name": "", "industry": "", "start_date": "", "end_date": ""})
    payload = json.loads(raw)
    rows = payload.get("data")
    if not isinstance(rows, list) or (rows and "ticker" not in rows[0]):
        raise RuntimeError(f"Feed format changed: keys={list(payload)[:10]}")
    out = []
    for r in rows:
        link = re.search(r'href="([^"]+)"', r.get("press_release") or "")
        out.append({
            "date": datetime.strptime(r["release_date"].strip(), "%d %b %Y").date().isoformat(),
            "ticker": (r.get("ticker") or "").strip(),
            "company": strip_tags(r.get("company_name")),
            "industry": strip_tags(r.get("industry_name")),
            "action": strip_tags(r.get("action_name")),
            "title": strip_tags(r.get("press_release")).rstrip("…").rstrip(),
            "link": html.unescape(link.group(1)) if link else "",
        })
    return out


def row_key(r):
    return "|".join([r["date"], r["ticker"], r["action"], r["title"][:80]])


def fetch_company(ticker):
    """Current corporate rating/outlook and full press-release titles by date."""
    page = http(COMPANY_URL.format(urllib.parse.quote(ticker)))
    info = {"rating": "", "outlook": "", "updated": "", "titles": {}}
    m = re.search(r'class="rating">(.*?)</span>', page, re.S)
    if m:
        info["rating"] = strip_tags(m.group(1)).replace(" ", "")
    m = re.search(r'class="outlook">(.*?)</span>', page, re.S)
    if m:
        info["outlook"] = strip_tags(m.group(1))
    m = re.search(r'class="price-update">(.*?)</span>', page, re.S)
    if m:
        info["updated"] = strip_tags(m.group(1)).replace("Updated on", "").strip()
    for d, t in re.findall(r'data-sort="(\d{4}-\d{2}-\d{2})".*?<a [^>]*>(.*?)</a>', page, re.S):
        info["titles"].setdefault(d, []).append(strip_tags(t))
    return info


# ---------------------------------------------------------------- classify

def classify(r):
    """Return alert kind or None."""
    a = r["action"].lower()
    t = r["title"].lower()
    if "downgrad" in a:
        return "DOWN"
    if "upgrad" in a:
        return "UP"
    if "withdraw" in a:
        return "WITHDRAWN"
    if "outlook" in a:
        return "OUTLOOK"
    if "credit watch" in t or "creditwatch" in t:
        return "WATCH"
    if not a:  # older rows have no action label; fall back to title wording
        if re.search(r"\b(lowered|downgraded|cut)\b", t):
            return "DOWN"
        if re.search(r"\b(raised|upgraded)\b", t):
            return "UP"
    return None


def parse_move(title):
    m = MOVE_RE.search(title)
    if not m:
        return None, None, None
    to_r, from_r = (normalise(m.group(1)), normalise(m.group(2)))
    notches = None
    if to_r in SCALE and from_r in SCALE:
        notches = SCALE.index(from_r) - SCALE.index(to_r)  # +ve = upgrade
    return from_r, to_r, notches


def normalise(r):
    return "id" + r[2:].upper() if r.lower().startswith("id") else r


def load_watchlist():
    raw = os.environ.get("WATCHLIST")
    if not raw and LOCAL_WATCHLIST.exists():
        raw = LOCAL_WATCHLIST.read_text(encoding="utf-8")
    if not raw:
        return {}, {}
    w = json.loads(raw)
    return ({k.upper(): v for k, v in w.get("tickers", {}).items()},
            {k.lower(): v for k, v in w.get("keywords", {}).items()})


def watch_tag(r, tickers, keywords):
    if r["ticker"].upper() in tickers:
        return tickers[r["ticker"].upper()]
    blob = (r["company"] + " " + r["title"]).lower()
    for k, v in keywords.items():
        if k in blob:
            return v
    return None


def enrich(alert):
    try:
        info = fetch_company(alert["ticker"])
    except Exception as e:  # enrichment is best-effort
        print(f"  enrich failed for {alert['ticker']}: {e}")
        return alert
    alert["corp_rating"] = info["rating"]
    alert["corp_outlook"] = info["outlook"]
    stem = alert["title"][:60].lower()
    for full in info["titles"].get(alert["date"], []):
        if full.lower().startswith(stem) or not stem:
            alert["title"] = full
            break
    return alert


def build_alert(r, kind, tickers, keywords, do_enrich=True):
    a = dict(r, kind=kind, key=row_key(r), watch=watch_tag(r, tickers, keywords))
    if do_enrich and a["ticker"]:
        a = enrich(a)
    a["from"], a["to"], a["notches"] = parse_move(a["title"])
    a["fallen_angel"] = bool(
        a["to"] in SCALE and a["from"] in SCALE
        and SCALE.index(a["to"]) > INVESTMENT_GRADE_FLOOR >= SCALE.index(a["from"]))
    return a


# ---------------------------------------------------------------- notify

ICON = {"DOWN": "⬇️", "UP": "⬆️", "OUTLOOK": "↔️", "WATCH": "👁️", "WITHDRAWN": "⛔"}
LABEL = {"DOWN": "DOWNGRADE", "UP": "UPGRADE", "OUTLOOK": "OUTLOOK CHANGE",
         "WATCH": "CREDIT WATCH", "WITHDRAWN": "WITHDRAWN"}


def headline(a):
    move = f" {a['from']} → {a['to']}" if a.get("to") else ""
    notch = ""
    if a.get("notches"):
        n = abs(a["notches"])
        notch = f" ({n} notch{'es' if n > 1 else ''})"
    return f"{ICON[a['kind']]} {a['ticker']} {LABEL[a['kind']]}{move}{notch}"


def priority(a):
    if a["watch"] and a["kind"] in ("DOWN", "WATCH"):
        return 5
    if a["watch"] or a["kind"] == "DOWN" or a["fallen_angel"]:
        return 4
    return 3


def send_ntfy(a):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    title = ("🔴 WATCHLIST · " if a["watch"] else "") + headline(a)
    lines = [a["company"]]
    if a["watch"]:
        lines.append(f"Held: {a['watch']}")
    if a["fallen_angel"]:
        lines.append("⚠️ Fell below investment grade (idBBB-)")
    if a.get("corp_rating"):
        lines.append(f"Corporate: {a['corp_rating']} / {a.get('corp_outlook') or '-'}")
    lines.append(f"{a['date']} · {a['action'] or 'n/a'}")
    lines.append(a["title"])
    company_page = COMPANY_URL.format(a["ticker"])
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data="\n".join(lines).encode(),
        headers={"Title": _hdr(title),
                 "Priority": str(priority(a)),
                 "Tags": "rotating_light" if priority(a) == 5 else "bell",
                 "Click": company_page,
                 "Actions": f"view, Company page, {company_page}"
                            + (f"; view, Dashboard, {DASHBOARD_URL}" if DASHBOARD_URL else "")})
    urllib.request.urlopen(req, timeout=30).read()


def _hdr(s):
    # ntfy accepts RFC 2047 encoded UTF-8 headers
    return "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


def send_ntfy_text(title, body, prio=3):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(),
                                 headers={"Title": _hdr(title), "Priority": str(prio)})
    urllib.request.urlopen(req, timeout=30).read()


def send_email(alerts, subject=None, intro=""):
    user, pw, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to):
        return
    alerts = sorted(alerts, key=lambda a: (-priority(a), a["ticker"]))
    if subject is None:
        top = alerts[0]
        subject = ("[PEFINDO] " + ("🔴 " if top["watch"] else "") + headline(top)
                   + (f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""))
    cards = []
    for a in alerts:
        colour = {"DOWN": "#c0392b", "UP": "#1e8449"}.get(a["kind"], "#7d6608")
        badges = ""
        if a["watch"]:
            badges += f'<span style="background:#c0392b;color:#fff;padding:2px 6px;border-radius:4px;font-size:12px">WATCHLIST · {html.escape(a["watch"])}</span> '
        if a["fallen_angel"]:
            badges += '<span style="background:#7d3c98;color:#fff;padding:2px 6px;border-radius:4px;font-size:12px">BELOW INVESTMENT GRADE</span>'
        corp = (f"<br>Corporate rating/outlook now: <b>{html.escape(a['corp_rating'])}</b> / "
                f"{html.escape(a.get('corp_outlook') or '-')}") if a.get("corp_rating") else ""
        pr = f' · <a href="{html.escape(a["link"])}">Press release</a>' if a.get("link") else ""
        cards.append(f"""
<div style="border-left:4px solid {colour};padding:8px 12px;margin:12px 0;font-family:Arial,sans-serif">
  <div style="font-size:16px;font-weight:bold;color:{colour}">{html.escape(headline(a))}</div>
  <div>{badges}</div>
  <div style="margin-top:4px"><b>{html.escape(a['company'])}</b> · {html.escape(a['industry'])}</div>
  <div style="color:#555;font-size:13px">{a['date']} · {html.escape(a['action'] or 'n/a')}{corp}</div>
  <div style="margin-top:6px">{html.escape(a['title'])}</div>
  <div style="margin-top:6px;font-size:13px"><a href="{COMPANY_URL.format(a['ticker'])}">Company page</a>{pr}</div>
</div>""")
    dash = f'<p><a href="{DASHBOARD_URL}">Open dashboard</a></p>' if DASHBOARD_URL else ""
    body = f"<div style='max-width:640px'>{intro}{''.join(cards)}{dash}<p style='color:#888;font-size:12px'>Source: PEFINDO rating action feed. Automated monitor.</p></div>"
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


# ---------------------------------------------------------------- dashboard data

def to_compact(r):
    kind = classify(r)
    frm, to, notches = parse_move(r["title"]) if kind else (None, None, None)
    return [r["date"], r["ticker"], r["company"], r["industry"], r["action"],
            r["title"], kind or "", frm or "", to or "", notches if notches is not None else ""]


def enrich_history(rows, days=730, pause=1.5):
    """Replace truncated feed titles with full ones for recent rating changes.

    One company-page request per ticker, spaced out; used on first run and weekly resync.
    """
    cutoff = (datetime.now(WIB) - timedelta(days=days)).date().isoformat()
    by_ticker = {}
    for r in rows:
        if r["date"] >= cutoff and r["ticker"] and classify(r):
            by_ticker.setdefault(r["ticker"], []).append(r)
    print(f"enriching {sum(map(len, by_ticker.values()))} changes across {len(by_ticker)} tickers")
    for ticker, items in by_ticker.items():
        try:
            titles = fetch_company(ticker)["titles"]
        except Exception as e:
            print(f"  {ticker}: {e}")
            continue
        for r in items:
            stem = r["title"][:60].lower()
            for full in titles.get(r["date"], []):
                if full.lower().startswith(stem):
                    r["title"] = full
                    break
        time.sleep(pause)
    return rows


def update_data(rows, full=False):
    DATA_FILE.parent.mkdir(exist_ok=True)
    existing = []
    if DATA_FILE.exists() and not full:
        existing = json.loads(DATA_FILE.read_text(encoding="utf-8"))["rows"]
    def key(e):  # title prefix, so an enriched full title matches its truncated feed version
        return "|".join([e[0], e[1], e[4], e[5][:60]])
    seen = {key(e) for e in existing}
    fresh = [c for c in map(to_compact, rows) if key(c) not in seen]
    merged = sorted(fresh + existing, key=lambda e: e[0], reverse=True)
    DATA_FILE.write_text(json.dumps({
        "updated": datetime.now(WIB).isoformat(timespec="minutes"),
        "columns": ["date", "ticker", "company", "industry", "action", "title",
                    "kind", "from", "to", "notches"],
        "rows": merged}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(fresh)


# ---------------------------------------------------------------- main

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": [], "failures": 0}


def save_state(state):
    state["seen"] = state["seen"][:SEEN_KEEP]
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def run(dry=False):
    state = load_state()
    tickers, keywords = load_watchlist()
    first_run = not state["seen"]
    rows = fetch_feed(8000 if first_run else POLL_ROWS)
    seen = set(state["seen"])
    new = [r for r in rows if row_key(r) not in seen]
    print(f"fetched {len(rows)} rows, {len(new)} new, watchlist {len(tickers)} tickers")

    if first_run:
        print("first run: seeding state, no alerts for history")
        alerts = []
    else:
        alerts = [build_alert(r, k, tickers, keywords, do_enrich=not dry)
                  for r in new if (k := classify(r))]

    for a in alerts:
        print(" ", ("[WATCH] " if a["watch"] else "") + headline(a), "|", a["company"], "|", a["title"][:100])

    if dry:
        return

    for a in alerts:
        send_ntfy(a)
    if alerts:
        send_email(alerts)
    if first_run:
        n_watch = len(tickers) + len(keywords)
        msg = (f"Monitoring {len(rows)} PEFINDO rating actions. Watchlist: {n_watch} names. "
               "You will be alerted on upgrades, downgrades, outlook changes, credit watch and withdrawals.")
        send_ntfy_text("✅ PEFINDO monitor is online", msg)
        send_email([], subject="[PEFINDO] Monitor is online", intro=f"<p>{msg}</p>")

    state["seen"] = [row_key(r) for r in new] + state["seen"]
    state["failures"] = 0
    state["last_success"] = datetime.now(WIB).isoformat(timespec="minutes")
    save_state(state)
    full_titles = {a["key"]: a["title"] for a in alerts}
    rows = [dict(r, title=full_titles.get(row_key(r), r["title"])) for r in rows]
    if first_run:
        rows = enrich_history(rows)
    added = update_data(rows, full=first_run)
    print(f"dashboard rows added: {added}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resync", action="store_true")
    p.add_argument("--test", metavar="TICKER")
    args = p.parse_args()

    if args.test:
        tickers, keywords = load_watchlist()
        rows = [r for r in fetch_feed(POLL_ROWS * 5) if r["ticker"].upper() == args.test.upper()]
        if not rows:
            sys.exit(f"no recent action for {args.test}")
        r = rows[0]
        a = build_alert(r, classify(r) or "WATCH", tickers, keywords)
        a["company"] = "[TEST] " + a["company"]
        print(headline(a), a)
        send_ntfy(a)
        send_email([a], subject="[PEFINDO TEST] " + headline(a))
        return

    if args.resync:
        n = update_data(enrich_history(fetch_feed(8000)), full=True)
        print(f"resynced dashboard data: {n} rows")
        return

    try:
        run(dry=args.dry_run)
    except Exception as e:
        state = load_state()
        state["failures"] = state.get("failures", 0) + 1
        # alert on 2nd consecutive failure, then roughly once a day (48 runs)
        if state["failures"] == 2 or state["failures"] % 48 == 0:
            try:
                send_ntfy_text("⚠️ PEFINDO monitor failing",
                               f"{state['failures']} consecutive failures. Last error: {e!r}", prio=4)
            except Exception:
                pass
        if not args.dry_run:
            save_state(state)
        raise


if __name__ == "__main__":
    main()
