# PEFINDO Rating Watch

Alerts on every PEFINDO rating change (upgrade, downgrade, outlook change, credit watch, withdrawal)
via phone push (ntfy) and email, plus a dashboard installable on a phone home screen.

## How it works

```
GitHub Actions (every 30 min)
  └─ checker.py ── POST pefindo.com/rating-action-reports/get-rating-action (public feed)
        ├─ new rating change? → fetch company page for full title + current rating/outlook
        ├─ ntfy push  (priority 5 for watchlist downgrades / credit watch)
        ├─ Gmail      (one email per run, watchlist items first)
        └─ commits state.json + docs/data.json → GitHub Pages dashboard
```

- Dedupe key: release date + ticker + action + title. `state.json` holds what has been seen.
- First run seeds history silently and sends a single "monitor is online" message.
- Sunday 03:00 WIB: full resync of dashboard data (full titles for the last 2 years of changes).
- If the feed fails twice in a row (or its format changes), a push alert says the monitor is failing.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `NTFY_TOPIC` | private ntfy topic name (subscribe to it in the ntfy app) |
| `SMTP_USER` | Gmail address that sends |
| `SMTP_PASS` | Gmail **app password** (myaccount.google.com/apppasswords) |
| `MAIL_TO` | recipient(s), comma separated |
| `WATCHLIST` | JSON `{"tickers": {"ABCD": "Bond"}, "keywords": {"some bank": "Deposit"}}` |

The watchlist (portfolio holdings) is kept only in the secret and in the local, gitignored
`watchlist.json`. The dashboard's watchlist is stored in each browser's local storage.

## Manual runs

Actions → *PEFINDO rating monitor* → Run workflow, with `mode`:

- `run` – normal check
- `dry-run` – print what would alert, send nothing
- `test` + `ticker` – send a test push and email for that ticker's latest action
- `resync` – rebuild dashboard data

## Updating the watchlist

Edit `watchlist.json` locally, then:

```bash
gh secret set WATCHLIST < watchlist.json
```

On the dashboard, open *Watchlist* at the bottom and paste `TICKER = label` lines.
