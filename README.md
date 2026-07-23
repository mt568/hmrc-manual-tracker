# hmrc-manual-tracker

Tracks wording-level changes to selected HMRC internal manuals published on
GOV.UK. A scheduled job fetches manual pages from the public GOV.UK Content
API, stores a normalised copy per page, and records detected changes in an
append-only event ledger. Git history is the version archive.

**This is guidance monitoring, not advice.** HMRC manuals are HMRC's internal
guidance — not legislation, and not personalised tax advice. Always check the
live page on GOV.UK before relying on anything here.

## Layout

- `manuals.yml` — tracked manual slugs
- `manifest/<slug>.json` — crawl manifest: known sections, ETags, change-note watermark
- `data/<slug>/<SECTION>.json` — canonical page content (body HTML + structure, volatile fields stripped)
- `view/<slug>/<SECTION>.md` — human-readable derived view (regenerable; converter pinned)
- `events/*.jsonl` — append-only detected-change ledger (what the site renders)
- `runs/*.jsonl` — append-only run ledger (observation windows, per-manual status)
- `scripts/scrape.py` — the whole pipeline; see its docstring
- `.github/workflows/track.yml` — daily crawl + commit + health check

Changes are reported as **detected between observation windows** (see
`runs/`), not as the exact moment HMRC edited a page.

## Licence and attribution

Manual content: © Crown copyright, reproduced under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
Source: [GOV.UK HMRC internal manuals](https://www.gov.uk/government/collections/hmrc-manuals).
Tracker code: MIT.
