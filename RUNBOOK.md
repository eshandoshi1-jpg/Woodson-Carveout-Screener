# RUNBOOK — Woodson Carveout Intelligence

One page, for whoever owns this after the analyst. No coding required to operate it.

## What it is
A private web tool that screens public companies for likely divestitures, names the division,
links each signal to its SEC filing, and attaches the outreach contact. Three tabs: **Overview**
(what we've found), **Explorer** (search all companies), **Outreach** (draft emails — copy only,
nothing sends).

## The URL
- **App:** _<paste the Vercel URL here once deployed>_
- Bookmark it. It's always the current data. It's private (password / SSO — see Access below).

## Reading the freshness chip (top-right)
- **Green** — "Data as of <date>": the snapshot is current.
- **Amber** — the last refresh is stale (>36h old). Something didn't run — do a manual refresh
  (below). If that fails, check https://www.sec.gov is up, then re-run once more.

## Refreshing the data (manual — takes a few minutes)
1. Open the GitHub repo → **Actions** tab → **"Refresh snapshot"**.
2. Click **Run workflow** → **Run workflow**. _(screenshot: TODO — add after first run)_
3. Wait for the green check. Vercel redeploys automatically; the URL shows the new date within a
   minute. (Automatic twice-daily refresh turns on once the Phase-2b crawl is wired — see below.)

## Who owns what
- **GitHub repo** and **Vercel project** must live under **firm-owned accounts**, not a personal one.
  (If they were created personally, transfer them before the analyst leaves — that's the one blocking
  handoff task.)
- **No personal secrets anywhere.** SEC/EDGAR needs no API key — only a declared User-Agent, set to a
  firm email (`WoodsonEquity research@woodsonequity.com`) in the workflow. The workflow uses only the
  repo's built-in token.

## Access (decide this explicitly)
The app is firm deal data at a URL. Turn on **Vercel Deployment Protection** (password) or the firm's
SSO. The `vercel.json` already sends `noindex`. Don't share the link outside the firm.

## What's automated vs. manual (current state)
- **Automated:** publish loop — any data change committed by the workflow redeploys to the same URL.
- **Manual for now:** the actual EDGAR re-crawl. Phase 2a ships the deploy + manual-refresh loop;
  **Phase 2b** (the incremental daily-index pipeline that updates the data unattended, twice daily)
  is the remaining engineering. Until it lands, "Run workflow" regenerates the site from the last
  full build; a fresh full build is run from a workstation with `python3 build_pipeline.py`.

## If something breaks
- Amber chip / stale data → Run workflow again.
- Workflow red → open the failed run, read the last red step. Most likely: EDGAR rate-limited
  (re-run in an hour) or a dependency install hiccup (re-run).
- App shows "Snapshot failed to load" → the snapshot didn't deploy; re-run the workflow.

## Handoff acceptance test
A person who has never seen the code, holding only this file, can (a) open the current data,
(b) run a manual refresh, and (c) explain the freshness chip. If any step needs the analyst,
the handoff isn't done.
