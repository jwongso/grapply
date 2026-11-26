# Discovery freshness and geography improvement

User request, 12 November 2026: improve the existing job discovery engine and Chrome companion instead of repeatedly recommending old postings. Berlin is preferred; Hamburg, Frankfurt and Munich are also eligible. StepStone is explicitly out of scope because of its access restrictions.

## Implementation and cleanup plan

1. Preserve the current extraction/generation/application flows. Keep discovery read-only against employer systems; do not submit applications, accept consents, bypass access controls, or add dependencies.
2. Lock the stale/reposted-job and office-location failures with regression tests. Record employer publication evidence separately from board dates, edit dates and first-seen timestamps. Never substitute Greenhouse updated_at for first_published. Ashby's publishedAt is last-publication evidence, not proof of the original posting date.
3. Default new discovery scans to seven days and a strict original-publication check. Keep uncertain dates/locations in an explicit review queue instead of silently labelling them fresh. Preserve a deliberate broader-scan override and existing saved configuration.
4. Reuse employer feeds to reconcile duplicate board copies. Prefer employer evidence over a longer aggregator description; preserve distinct seniority/location variants. Permit bounded verification of supported public employer URLs, not arbitrary user-controlled fetches.
5. Add the four approved German cities as explicit markets, with Berlin preferred, and distinguish remote from hybrid. Reject conflicts where an office-attendance requirement contradicts the advertised location. Do not infer worldwide eligibility from generic remote benefits.
6. Extend the existing discovery page/extension affordance with dates, evidence links, review reasons and a seven-day scan control. Do not add a second application engine. Expose safe browser-search links for the included boards if useful, not StepStone scraping.
7. Run existing and new offline tests, JavaScript syntax checks, and a browser UI smoke check. Use small read-only live probes only for public metadata; isolate test state from ~/.grapply and do not restart the user's running companion.

## Acceptance examples

- An old Fender/Greenhouse posting edited this week is old, not fresh.
- A board reposted today cannot override an older employer first-publication date.
- Unknown original dates and last-published-only dates remain visibly unverified in strict mode.
- A hybrid Munich posting is not advertised as Germany-wide remote merely because isRemote is true.
- Hamburg, Frankfurt and Munich onsite/hybrid work can qualify independently; Berlin remains first preference.
- A listing claiming Berlin but requiring a different office goes to review rather than silently passing.
- Discovery never auto-applies or generates an unselected application.

## Handoff and verification

- Changed engine files: `companion/discovery.py`, `companion/sources.py`.
  Changed discovery-only service/UI paths: `companion/main.py`,
  `extension/popup/popup.js`. Added evidence and UI regressions alongside the
  existing discovery tests, and documented usage in `README.md`.
- Simplification: a single post-dedupe freshness decision replaces premature
  per-source filtering. Original publication, edit, creation, board and
  observation timestamps now have distinct meanings. Uncertain candidates
  do not consume LLM scoring budget.
- Latest broad non-live selection: 262 passed, 9 skipped, 3 deselected.
  Command: `./.venv/bin/python -m pytest -q -m 'not integration'
  --ignore=tests/test_real_pages.py --ignore=tests/test_seek_real_page.py`.
  Includes JavaScript behavior tests and a mocked headless-Chrome smoke test.
- Python compilation, `node --check extension/popup/popup.js` and
  `git diff --check` passed. Ruff, mypy and pyright are not installed; no new
  dependencies were installed and no dedicated lint/type-check pass is claimed.
- Read-only public URL probe: Fender requisition 8288981 returned original
  publication 19 October 2026 and was correctly classified old. Other boards
  are covered with fixtures, not a live end-to-end scan. Keyword/context and
  office-conflict matching remain conservative heuristics, not a full semantic
  interpretation of every description. Missing original dates reduce the
  strict shortlist; use the visibly separate review queue for follow-up.
- An initial pre-change broad test run exposed an existing unmarked live Seek
  test race (`test_real_seek_page_extract_and_parser`). That test fetched a live
  page and generated `~/.grapply/parsers/nz.seek.com.py`; its cache entry was
  left in place rather than deleting or overwriting user-owned cache state.
  Subsequent broad runs excluded both live-page modules. No live discovery
  scan, saved-search migration, application submission or service restart was
  performed. Reload the companion and unpacked extension to activate changes.
- Shared application policies were updated only for the explicitly expanded
  city scope, stack-agnostic eligibility and StepStone exclusion. Historical
  application documents were not changed.

## Proposed employer-first scan

Poll a curated roster of 15–25 employers with Berlin engineering teams or
explicit Germany-remote hiring. Start with navigation and embedded systems,
developer tooling, and applied AI teams. HERE, TomTom, Ableton, Parloa, Langdock
and Zalando provide starting points. Find smaller employers in the Berlin AI
directory, then verify each careers URL on the employer site. An EU-remote role
qualifies when the employer includes Germany as a permitted work location.
[Berlin AI directory](https://berlin-ai.de/)

1. Extend the employer registry with verified careers URLs and reuse the
   existing ATS adapters for public feeds. Keep LinkedIn and Indeed as
   supplemental sources. Flag saved configurations with `max_age_days: 0`,
   which disables the age cutoff even when strict date verification is enabled.
2. Use a bounded careers crawl for employers without a usable feed. Prioritise
   listing pages and requisition URLs, obey robots.txt, and preserve the
   crawler's host boundary. Hand recognised external ATS links to the existing
   adapters. Extract job metadata from detail pages before cleaning HTML.
3. Reconcile records by employer and requisition ID, with a canonical posting
   URL as a fallback. Retain the earliest observation across aliases. Keep
   original publication evidence separate from edit, last-publication and
   first-seen timestamps. Greenhouse `first_published` and a credible employer
   `JobPosting.datePosted` can establish freshness. Ashby `publishedAt` records
   the last publication. Send unresolved or conflicting dates to review.
   [Greenhouse API](https://docs.greenhouse.io/job-board.html) ·
   [Ashby API](https://developers.ashbyhq.com/docs/public-job-posting-api) ·
   [JobPosting fields](https://developers.google.com/search/docs/appearance/structured-data/job-posting)
4. Score C++ systems roles for navigation, SDK, embedded integration and
   performance evidence. Score applied-AI roles for production retrieval,
   inference, model integration and evaluation. Check mandatory requirements
   before recommending a role. A language-agnostic production RAG fixture
   scored 26.9 against the current prefilter cutoff of 45, below the threshold
   for résumé scoring.

A three-page crawl of Zalando's engineering listings surfaced 173 job-shaped
URLs in the site map and spent its fetch budget on other pages. Test careers
prioritisation with a similarly small budget. Also test a refreshed board copy
of an old requisition, an EU-remote role restricted to another country, and a
language-agnostic applied-AI role with strong candidate evidence. During the
pilot, track verified fresh matches, dated coverage by employer and the first
observation on each source to measure any lead over the boards checked.
[Zalando engineering listings](https://jobs.zalando.com/en/jobs?category=Software+Engineering)
