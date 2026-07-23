# hmrc-manual-tracker

Tracks wording-level changes to selected HMRC internal manuals published on
GOV.UK. A scheduled job fetches manual pages from the public GOV.UK Content
API, stores a normalised copy per page, and records detected changes in an
append-only event ledger. Git history is the version archive.

The tracker checks manual roots and HMRC change notes daily. It also verifies
one-seventh of every known manual each day, so the complete known corpus is
checked approximately weekly. Silent edits can therefore take up to seven days
to appear. This is daily monitoring with weekly full verification, not a daily
full scrape.

**This is guidance monitoring, not advice.** HMRC manuals are HMRC's internal
guidance — not legislation, and not personalised tax advice. Always check the
live page on GOV.UK before relying on anything here.

## Layout

- `manuals.yml` — tracked manual slugs
- `manifest/<slug>.json` — known sections, ETags, watermark and discovery queue
- `data/<slug>/<SECTION>.json` — canonical page content (body HTML + structure, volatile fields stripped)
- `view/<slug>/<SECTION>.md` — human-readable derived view (regenerable; converter pinned)
- `events/*.jsonl` — detected changes with source, hashes and bounded text patch
- `runs/*.jsonl` — append-only run ledger (observation windows, per-manual status)
- `scripts/scrape.py` — the whole pipeline; see its docstring
- `tests/test_scrape.py` — fake-API tests for the ingestion state machine
- `.github/workflows/track.yml` — daily crawl + commit + health check

Changes are reported as **detected between observation windows** (see
`runs/`), not as the exact moment HMRC edited a page. New events use `via` to
identify HMRC change notes, the rolling sweep, structural discovery or initial
seeding. Legacy seed events are identified by a `seed-` run-id prefix. Seed
events establish the baseline and must not be displayed as subsequent HMRC
changes. Events labelled `via: simulation` are Phase-0 safety tests and are
also excluded.

## Local commands

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/scrape.py seed <manual-slug>
python scripts/scrape.py daily
python scripts/scrape.py health
```

Set `TRACKER_CONTACT` to a project contact URL or email address before making
live requests. The GitHub workflow supplies the repository issues URL.

## Licence and attribution

Manual content: © Crown copyright, reproduced under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
Source: [GOV.UK HMRC internal manuals](https://www.gov.uk/government/collections/hmrc-manuals).
Tracker code: MIT; see `LICENSE`.
