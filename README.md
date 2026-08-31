# Woodson Carveout Intelligence

A carveout deal-sourcing tool for Woodson Equity. It reads public SEC filings across the firm's
corporate list, scores each company's likelihood of divesting a division, names the specific division
(and checks it against Woodson's size box), links every signal to its source filing, and attaches the
outreach contact. The front end is a single self-contained web app.

**Operators:** see [`RUNBOOK.md`](RUNBOOK.md) — one page, no coding required.

**Deferred CRM/send integration:** see [`HUBSPOT_INTEGRATION_PLAN.md`](HUBSPOT_INTEGRATION_PLAN.md).

## Layout
- `web/` — the deployed app (`index.html` + `snapshot.json` + `vercel.json`). **Vercel root directory = `web/`.**
- `refresh.py` — the refresh entrypoint: `pipeline_incremental` → `export_snapshot` → `build_web`.
- `pipeline_incremental.py` — reads EDGAR's daily index, re-scans only companies that filed something
  new, merges into `data/woodson_enriched.xlsx`, advances `data/state.json` (high-water mark).
- `export_snapshot.py` → `data/snapshot.json` (the app's data contract).
- `build_web.py` → rebuilds `web/index.html` + `web/snapshot.json` from `data/woodson_app.template.html`.
- The Outreach Queue supports safe multi-select, batch review, CSV draft export, and a sidebar
  view of companies marked contacted. It never sends email.
- Banker Relationships contains 518 investment banks and 1,914 banker contacts, with firm and person
  pages for priority, owner, coverage, status, last touch, next follow-up, notes, outreach drafts, and
  activity export. The supplied firm list is classified as 391 Tier 4 banks; all other firms are the
  127 Tier 3 banks. Relationship activity is browser-local until a shared CRM connection is available.
- `build_pipeline.py` — the full (slow) rebuild of the whole universe; run locally, not in CI.
- Committed inputs (no external files needed at refresh time): `data/universe.csv` (CIK filter + re-scan
  inputs), `data/contacts.csv` (the corporate rolodex), and `data/banker_crm.json` (the banker directory).

## Refresh
- **Automatic:** `.github/workflows/refresh.yml` runs `refresh.py` every morning at 06:17 ET, plus a
  13:17 ET weekday backup, and commits the new snapshot; Vercel auto-deploys. Saturday morning catches
  the finalized Friday SEC index. Enable Actions on the repo for this to run.
- **Manual:** GitHub → Actions → "Refresh snapshot" → Run workflow. Or locally: `python3 refresh.py`.

## Requirements
Python 3.11+, `pip install -r requirements.txt`. SEC access needs no key — only a declared User-Agent,
set via the `SEC_USER_AGENT` env var (firm email; defaults to `WoodsonEquity research@woodsonequity.com`).

## Deploy (Vercel)
Import this repo, set **Root Directory = `web/`**, deploy. Turn on Deployment Protection (password/SSO).
`web/vercel.json` already sends `noindex` and `no-store` on the snapshot.
