# HubSpot integration plan (no paid upgrade)

## Decision

Woodson will keep HubSpot Free. The screener remains the research, selection,
and approval interface. HubSpot remains the company/contact system of record.
The connected Gmail or Microsoft 365 mailbox will eventually send approved
emails through its own API; HubSpot Free will not be used as a bulk sender.

No new target is ever sent automatically. A user must select companies, review
the final recipients and copy, and approve each batch.

## Workflow

1. The SEC pipeline refreshes the target universe and draft outreach.
2. The operator selects eligible firms in the Outreach Queue.
3. A preflight check blocks duplicates, prior outreach, missing contacts,
   unsupported geographic language, and unverified hard signals.
4. The operator reviews the complete batch and approves it once.
5. A protected server-side queue sends one personalized email per recipient at
   a controlled rate through the authorized mailbox.
6. The system writes sent, failed, bounced, replied, and follow-up status to
   HubSpot and the screener.

## Credentials required before live use

### HubSpot

A HubSpot Super Admin must create a private app for portal `24317311` with only:

- `crm.objects.companies.read`
- `crm.objects.companies.write`
- `crm.objects.contacts.read`
- `crm.objects.contacts.write`

Store the token as a protected server-side environment variable named
`HUBSPOT_ACCESS_TOKEN`. Never commit it, embed it in the snapshot, or place it
in browser JavaScript.

The current account navigation exposed only Development / Legacy Apps, and the
available user did not have permission to access private apps. Resolve the
Super Admin / current developer-platform path before live HubSpot work.

### Mailbox

Confirm whether the sender uses Google Workspace or Microsoft 365. The mailbox
owner must complete one OAuth approval after the connection page exists. The
application must never request or store the user's password, passkey, or MFA
code. The mailbox connection already present in HubSpot cannot be extracted or
reused by this separate application.

## Architecture still to build

- Protected server endpoints; no credentials in the static Vercel site.
- Firm-authenticated access to selection and send actions.
- Persistent outreach batches and message-level status.
- Idempotency keys so retries cannot duplicate sends.
- A paced background worker with pause and retry controls.
- HubSpot company/contact matching and activity logging.
- Google or Microsoft OAuth and send adapter.
- Bounce/reply synchronization; open tracking is optional and non-authoritative.

## Safe work that does not require credentials

- Geographic candidate-name suppression.
- Batch selection and final review in draft-only mode.
- Shared eligibility rules and preflight warnings.
- Dry-run exports and automated tests.

## Go-live sequence

1. Dry run with no external sends.
2. Read-only HubSpot matching.
3. Create one test company/contact.
4. Send only to internal Woodson addresses.
5. Send a small, explicitly approved external batch.
6. Verify HubSpot logging, replies, bounces, and duplicate protection.
7. Enable full approved batches.
