# RUNBOOK — Woodson Carveout Intelligence

One page, for whoever owns this after the analyst. No coding required to operate it.

## What it is
A private web tool that screens public companies for likely divestitures, names the division,
links each signal to its SEC filing, attaches the outreach contact, and manages investment-banker
relationships. **Today**, **Carveout Targets**, and **Outreach Queue** handle corporate outreach.
**Investment Banks** and **Bankers** organize 518 firms and 1,914 individual banker contacts.
The supplied firm list is Tier 4 (391 banks); every remaining firm is Tier 3 (127 banks). Tier views
are available in the Relationships section and bankers inherit their firm's tier.

Geographic reporting segments are intentionally withheld from outreach language. Those firms remain
available for general portfolio-review outreach, but the region itself is not described as non-core.

Each corporate target page has **Company details for DealCloud** with a copyable Name, Website, and
Description. The Outreach draft page links directly to those fields. A website is shown only when it
can be derived from the verified corporate contact's email domain; the tracker does not guess missing URLs.
Descriptions are intentionally limited to one plain business sentence using the original corporate-list
industry, with no ticker, CIK, transaction thesis, signals, or financial detail.

When a draft is marked contacted, the company appears under **Saved Views → Contacted companies** in
the sidebar. That status currently belongs to the browser that marked it; the future HubSpot sync will
make it shared across users and devices.

Banker pages include the approved introductory draft, relationship status, priority, owner, coverage,
last contact, next follow-up, and notes. Marking a banker contacted creates a follow-up 14 days later.
The tracker suggests High, Medium, or Low priority from the bank's tier and firm-level priority, the
banker's seniority, email availability, and relationship timing. A priority selected manually on the banker record overrides the
suggestion. **Today** shows the high-priority outreach list and follow-ups due.
Use **Download activity** as a backup because banker relationship history also belongs to the browser
until the shared HubSpot or database connection is available. The outdated firm overview is not stored
in the tracker; attach an updated overview before using the banker draft's attachment language.

## The URL
- **App:** https://woodson-carveout-screener.vercel.app/
- Bookmark this permanent address. Do not bookmark a longer Vercel preview address because previews
  are fixed to one deployment. The app redirects preview links to this current production version.

## Reading the freshness chip (top-right)
- **Green** — "Data as of <date>": the snapshot is current.
- **Amber** — the last refresh is stale (>36h old). Something didn't run — do a manual refresh
  (below). If that fails, check https://www.sec.gov is up, then re-run once more.

## Refreshing the data (manual — takes a few minutes)
1. Click **Refresh data** in the app header.
2. Click **Start secure refresh**. On the protected GitHub page, choose **Run workflow**.
3. Return to the tracker and leave the refresh panel open. It checks for the newly published snapshot
   and reloads automatically. **Check for update** is also available if you want to check immediately.

The same refresh runs every morning, with a second morning attempt and a weekday afternoon fallback.
No GitHub credential is stored in the browser.

## Who owns what
- **GitHub repo** and **Vercel project** must live under **firm-owned accounts**, not a personal one.
  (If they were created personally, transfer them before the analyst leaves — that's the one blocking
  handoff task.)
- **No personal secrets anywhere.** SEC/EDGAR needs no API key — only a declared User-Agent, set to a
  firm email (`WoodsonEquity research@woodsonequity.com`) in the workflow. The workflow uses only the
  repo's built-in token.

## Access (decide this explicitly)
The app is firm deal data at a URL. Access protection is not enabled yet. Before enabling it, choose
either **Vercel Deployment Protection** with a shared password or the firm's SSO and confirm who needs
access so the team is not locked out. The `vercel.json` already sends `noindex`. Don't share the link
outside the firm.

## What's automated vs. manual (current state)
- **Automated:** every morning, with morning and weekday-afternoon fallbacks, the workflow reads new EDGAR daily
  indexes, rescans only affected
  companies, publishes the updated snapshot, and lets Vercel redeploy the same URL. A failed refresh
  is retried twice before the workflow is marked red.
- **Manual fallback:** use **Run workflow** to start the same incremental refresh immediately. The full
  universe rebuild remains a workstation-only maintenance operation (`python3 build_pipeline.py`).

## If something breaks
- Amber chip / stale data → Run workflow again.
- Workflow red → open the failed run, read the last red step. Most likely: EDGAR rate-limited
  (re-run in an hour) or a dependency install hiccup (re-run).
- App shows "Snapshot failed to load" → the snapshot didn't deploy; re-run the workflow.

## Handoff acceptance test
A person who has never seen the code, holding only this file, can (a) open the current data,
(b) run a manual refresh, and (c) explain the freshness chip. If any step needs the analyst,
the handoff isn't done.
