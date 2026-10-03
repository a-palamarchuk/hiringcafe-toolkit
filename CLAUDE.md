# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.12+, managed with uv. Everything runs through `uv run`.

```bash
make install   # uv sync --extra dev + pre-commit install
make lint      # ruff check, ruff format --check, mypy (strict, over src/ tests/ scripts/)
make format    # ruff autofix + format
make test      # pytest
make check     # lint + test (what CI runs)

uv run pytest tests/test_job_shortlist_diff.py              # one file
uv run pytest tests/test_job_shortlist_diff.py -k some_name # one test

make shortlist # the full daily job-shortlist run (5 stages, stops on first failure)
```

mypy is `strict = true` and includes tests and scripts, so new code needs full annotations.
Line length is 100.

## Architecture

Two pipelines over hiring.cafe's undocumented job-search API, sharing a client and a scrape stage:

- `api/client.py` - mimics the site's own frontend: reads the Next.js build id from the search
  page, then pages through `/_next/data/<build_id>/<route>.json` (route taken from the page the
  landing request rendered, currently `classic`), refreshing the build id if it goes stale
  mid-run. A redirect payload is a stale build, never an empty page.
- `common/` - config loading (`config.py`), JSONL read/write with transparent gzip (`jsonl.py`),
  the shared scrape stage (`scrape.py`, which depends on a `PageSource` protocol so tests never
  need HTTP), host normalization and domain matching (`urls.py`), careers-link derivation from
  ATS apply URLs (`careers_link.py`), geo distance (`location.py`), and a `PageSource` over
  browser captures (`capture.py`).
- `extensions/hiringcafe-capture/` - Firefox extension that saves result pages browsed by hand,
  for days hiring.cafe serves the scraper a Cloudflare challenge. It must only observe: every
  page request comes from the user (a click, or one Alt+N press clicking the site's own link) -
  no timers, loops, or requests of its own. `job-shortlist import` replays its capture through
  the scrape stage; a capture may differ from its configured search only in the fetch window.
- `company_discovery/` - scrape -> rollup -> filter -> render. Unit is the company; filters are
  loose on purpose (a false negative costs a company you never hear about).
- `job_shortlist/` - scrape -> normalize -> screen -> diff -> render. Unit is the posting; run
  daily.
- `cli.py` - one Typer sub-app per pipeline, one command per stage.

Key conventions that span files:

- **Stages communicate only through timestamped files in `data/<pipeline>/{raw,interim,processed}/`.**
  Each stage defaults its input to the newest matching file from the previous stage (`_newest` in
  `cli.py`), and every stage writes a `*-meta-<timestamp>.json` sidecar recording counts and
  decisions so silent truncation or bad filtering is visible. Chain stages with `&&`/make, never
  `;`, or a failed stage leads the rest to reprocess stale data.
- **Raw records are stored verbatim**; `job_shortlist/normalize.py` projects them onto a flat
  `Posting` schema so vendor field renames break one function loudly instead of silently dropping
  postings downstream.
- **Screening has three bands** (strong / possible / rejected) with *all* applicable
  `reject_reasons` / `demote_reasons` recorded, not the first match. Rejected postings are still
  written out.
- **`data/job_shortlist/state/seen.jsonl` is the only non-regenerable file.** `diff.py` identifies
  a posting by every merged listing id plus the vendor cluster key; content-derived keys were
  tried and rejected. Re-surfacing happens only on band promotion.
- **VisitLogger integration** (a separate Firefox extension): rendered HTML marks queued links
  with `data-visit-open`, `data-visit-key`, `data-visit-mark`. Employer links are keyed by company
  host (shared between both pipelines, so they must render the same URL for that key); posting
  links are keyed `job:<objectID>`. Links on shared ATS hosts must never be keyed by that host.
  The visit-log export is read back for `opened` / `applied` / `company_applied` labels.
- **Erring direction is deliberate**: dedup under-merges, suppression under-suppresses, because a
  duplicate costs a glance while a wrong merge/suppression hides a job forever.

The README documents the reasoning (and measured numbers) behind most rules; read the relevant
section before changing a threshold or matching rule.

## Configuration

Real configs (`config/*.toml`, `config/searchstates/*.json`) are gitignored; `*.example.*`
counterparts are checked in. The search definition is the URL-decoded `searchState` query param
copied verbatim from the hiring.cafe UI, not hand-written. `data/` contents are gitignored.

## scripts/

Standalone measurement tools (`inspect_scrape.py`, `inspect_dedup.py`, `screen_funnel.py`) run
against raw scrape files to calibrate rules before they go into pipeline stages. They are not part
of the pipeline but are type-checked.
