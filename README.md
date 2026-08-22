# hiringcafe-toolkit

![CI](https://github.com/a-palamarchuk/hiringcafe-toolkit/actions/workflows/ci.yml/badge.svg)

Personal pipelines for company and job-posting discovery, built on hiring.cafe's job-search API.

## Why this exists

Two different questions need two different tools.

**Company discovery** asks *which companies near me employ engineers?* Individual postings are
only evidence; the output is a list of company career pages to review, deduplicated against
pages already visited. Filters here are deliberately loose, because a false positive costs one
glance while a false negative costs a company you never hear about.

**Job shortlist** asks *what was posted today that I should apply to?* The unit is the posting,
filters are tight, and the run is daily.

Both read the same API, so the client and shared helpers live in `api/` and `common/`, while
each pipeline keeps its own stages and lifecycle.

## Project layout

```
hiringcafe-toolkit/
├── src/hiringcafe_toolkit/
│   ├── api/                   # hiring.cafe API client, shared across pipelines
│   ├── common/                # config loading, serialization, shared models
│   ├── company_discovery/     # Scrape -> Rollup -> Filter -> Render career-page discovery
│   ├── job_shortlist/         # Daily scrape and shortlist of new job postings
│   └── cli.py                 # `hiringcafe-toolkit` entry point
├── config/                    # *.example.toml checked in; real *.toml gitignored
├── data/                      # generated datasets; structure tracked, contents gitignored
│   ├── company_discovery/{raw,interim,processed}/
│   ├── job_shortlist/{raw,interim,processed}/
│   └── inputs/                # local inputs, e.g. the VisitLogger export
├── tests/                     # mirrors src/ layout
└── .github/workflows/         # CI: lint, type-check, test
```

Each stage is a separate module and a separate CLI command rather than one script, so stages can
be run, tested, and scheduled independently. Changing a filter should never mean re-scraping.

## How the API access works

hiring.cafe publishes no documented API. This toolkit makes the same requests the site's own
frontend makes: it reads the search page for the current Next.js build id and first page of
results, then walks `/_next/data/<build_id>/index.json` for subsequent pages. The build id
changes on every deploy, so it is read at run time and refreshed automatically if it goes stale
mid-run.

Because the interface is undocumented, it can change without notice. Two consequences shape the
design: raw responses are stored verbatim so later stages can be rewritten without re-scraping,
and every run writes a meta sidecar recording page counts and the reason iteration stopped, so a
truncated run is visible rather than silent.

This is a personal-scale tool. Requests are sequential with a delay between pages, and there is a
hard page ceiling per run.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/a-palamarchuk/hiringcafe-toolkit.git
cd hiringcafe-toolkit
make install
```

Common tasks (see `Makefile`):

```bash
make lint    # ruff + mypy
make format  # ruff format, with autofix
make test    # pytest
make check   # lint + test
```

## Configuration

Scalar settings live in `config/<pipeline>.toml`, gitignored because they hold personal search
parameters. Copy the matching `config/<pipeline>.example.toml` and fill in real values.

The search definition itself is **not** written by hand. It is a deeply nested object copied out
of the hiring.cafe page URL:

1. Build the search in the hiring.cafe UI, setting every filter you want.
2. Copy the `searchState` query parameter out of the browser's address bar.
3. URL-decode it and save the result as JSON under `config/searchstates/`.
4. Point `[search].searchstate_path` at that file.

Keeping it verbatim means the search that ran is exactly the search you built. Hand-translating
filters into TOML would invite silent errors, and a mistyped filter produces a plausible-looking
result set that is quietly wrong.

## Usage

### Company discovery

```bash
# Stage 1: fetch raw job records for the configured search
uv run hiringcafe-toolkit company-discovery scrape

# Overrides, e.g. for a smoke run before committing to a full one
uv run hiringcafe-toolkit company-discovery scrape --max-pages 3
```

Commands are shown with `uv run`, which executes them inside the project's virtual environment.
Run `source .venv/bin/activate` once instead if you prefer the bare `hiringcafe-toolkit` command.

Each run writes two files to `data/company_discovery/raw/`:

| File | Contents |
|---|---|
| `jobs-<timestamp>.jsonl` | One raw record per line, deduplicated by `objectID`, otherwise untouched |
| `meta-<timestamp>.json` | searchState used, per-page received/new counts, build ids, reported totals, stop reason |

After a run, check `meta-*.json` before trusting the results:

- `stop_reason` of `empty page` means the API ended the sequence on its own.
- `reached max_pages=N` means the run was cut short; re-run with a higher ceiling.
- `unique_records` against `reported_totals` shows how much of the claimed result set was
  actually retrieved. Pagination can thin out before the reported total is reached; when that
  matters, split the search into narrower ones.

### Stage 2: rollup

```bash
uv run hiringcafe-toolkit company-discovery rollup
```

Aggregates the newest raw scrape into one record per company, writing to
`data/company_discovery/interim/`:

| File | Contents |
|---|---|
| `companies-<timestamp>.jsonl` | One company per line: name, website, careers link, distance, locations, posting counts, sample titles |
| `companies-no-website-<timestamp>.jsonl` | Companies with no usable website, which cannot be deduplicated against a visit log recorded by domain |
| `rollup-meta-<timestamp>.json` | Drop counts by reason, careers-link derivation coverage, and a diagnostic listing domains that mapped to more than one company name |

What it drops, and why:

- **Excluded sources.** Public-sector boards are agencies, counties, and school districts rather
  than companies with a career page worth reviewing.
- **Postings with no qualifying location.** The scrape matches a posting if *any* of its
  locations is in range, so a posting listing College Park and Boulder can arrive with neither
  near home. Each workplace city is re-checked against the radius and the excluded states, and
  the posting survives only if one qualifies. A posting listing both an excluded state and a
  nearby city is kept, on the strength of the nearby one.
- **Public-sector and university domains.** Source exclusion cannot catch an agency or
  university that uses a mainstream ATS, so `.gov`, `.mil`, and `.edu` websites are excluded by
  domain as well.
- **Expired postings.**

Postings with no location data at all are kept, with an unknown distance, rather than dropped on
missing evidence.

The `ambiguous_domains` diagnostic is worth a glance after each run: it lists website hosts that
resolved to more than one company name, which is how a wrong merge (subsidiaries sharing a
domain) would show up.

### Stage 3: filter

```bash
uv run hiringcafe-toolkit company-discovery filter
```

Drops companies whose website already appears in the
[VisitLogger](https://github.com/a-palamarchuk/visit-logger) export, writing to
`data/company_discovery/interim/`:

| File | Contents |
|---|---|
| `remaining-<timestamp>.jsonl` | Companies still worth a visit |
| `remaining-no-website-<timestamp>.jsonl` | The website-less rows, passed through unfiltered |
| `filter-meta-<timestamp>.json` | Counts, unusable visit-log keys, and a sample of what was excluded |

Both the export keys and the company websites go through the same host normalization, so
`www.Acme.com` in the log matches `acme.com` in the data.

Rows without a company website cannot be filtered: they are keyed on an ATS URL, and marking one
visited would record a host shared by thousands of unrelated employers. They pass through to
their own file, which is why that file repeats in full on every run rather than shrinking as you
work through it.

The `excluded_sample` field in the meta is worth a glance on the first run: if host normalization
ever breaks, it shows up there as obviously wrong matches rather than as a silently smaller
output file.

### Stage 4: render

```bash
uv run hiringcafe-toolkit company-discovery render
```

Writes the visit list to `data/company_discovery/processed/` as HTML, sorted nearest first with
unknown distances last:

| File | Contents |
|---|---|
| `companies-<timestamp>.html` | The main list |
| `companies-no-website-<timestamp>.html` | The rows with no company website |

Open the page in Firefox and start the
[VisitLogger](https://github.com/a-palamarchuk/visit-logger) tab queue from its context menu.

Each row carries exactly one queued link - the careers page - marked with the company's own
website host:

```html
<a href="https://job-boards.greenhouse.io/acme/"
   data-visit-open data-visit-key="acme.com" data-visit-mark="auto">careers</a>
```

The key matters because careers pages often live on a vendor host shared by thousands of
employers. Keying the link to the company's own domain is what lets the visit log deduplicate
correctly, and what lets an interrupted pass resume: rows already opened are skipped next time.

The company and search links on the same row are plain anchors, so they never enter the queue.
They are what you reach for when a derived careers URL turns out to be wrong - the derivation is
a heuristic, and a 404 is expected occasionally.

Rows in the second file have no company domain to key against, so their links open without
marking anything. That list repeats in full on every run and is worked through by eye; when you
find a company's real site, mark it there.

## License

MIT. See [LICENSE](LICENSE).
