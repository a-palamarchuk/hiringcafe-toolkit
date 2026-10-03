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

Both read the same API, so the client, the scrape stage, and shared helpers live in `api/` and
`common/`, while each pipeline keeps its own later stages and lifecycle. Scraping is identical
either way - fetch pages, deduplicate by `objectID`, write raw records and a meta sidecar - so
the pipelines diverge only after the raw data lands.

## Project layout

```
hiringcafe-toolkit/
├── src/hiringcafe_toolkit/
│   ├── api/                   # hiring.cafe API client, shared across pipelines
│   ├── common/                # config loading, serialization, scrape stage, shared models
│   ├── company_discovery/     # Scrape -> Rollup -> Filter -> Render career-page discovery
│   ├── job_shortlist/         # Daily scrape and shortlist of new job postings
│   └── cli.py                 # `hiringcafe-toolkit` entry point
├── extensions/
│   └── hiringcafe-capture/    # Firefox extension: save result pages browsed by hand
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
results, then walks `/_next/data/<build_id>/<route>.json` for subsequent pages. The build id
changes on every deploy, so it is read at run time and refreshed automatically if it goes stale
mid-run.

The route is read at run time as well, from the page the search request lands on. Around
2026-09-30 the search page moved from `/` to `/classic`; the old `index.json` route then
answered every page with a redirect and no records, which the scrape took for the end of the
results and stopped after the first page. A redirect payload is now treated as a stale build -
refresh, retry, and fail loudly if it persists - never as an empty page.

Because the interface is undocumented, it can change without notice. Two consequences shape the
design: raw responses are stored verbatim so later stages can be rewritten without re-scraping,
and every run writes a meta sidecar recording page counts and the reason iteration stopped, so a
truncated run is visible rather than silent.

This is a personal-scale tool. Requests are sequential with a delay between pages, and there is a
hard page ceiling per run.

### The `dateFetchedPastNDays` parameter

Despite appearing in the UI as a dropdown of preset ranges, this is a literal count of days,
filtering on when hiring.cafe's crawler **fetched** a posting - not when the employer published
it. The UI pads each preset by roughly one extra unit of its own granularity, to cover the lag
between a job going live and the crawler indexing it.

| UI label | Value | UI label | Value |
|---|---|---|---|
| All time | -1 | 3 months | *(omitted)* |
| Past 24 hours | 2 | 4 months | 151 |
| 3 days | 4 | 5 months | 181 |
| 1 week | 14 | 6 months | 211 |
| 2 weeks | 21 | 1 year | 750 |
| 3 weeks | 29 | 2 years | 1080 |
| 1 month | 61 | 3 years | 1440 |
| 2 months | 91 | | |

Omitting the field entirely is the "3 months" option, which by the padding rule is a ~121-day
window. An absent value is therefore a wide default, not an unbounded search.

Because the padding is baked into the value, the parameter is a poor proxy for posting freshness.
Filter on `estimated_publish_date` downstream instead.

Preset values are not believed to be the only valid ones - the parameter looks continuous - but
that is untested.

### Compensation filters

`maxCompensationLowEnd` bounds the low end of a posting's *maximum* compensation: setting it to
`"190000"` (a string, not a number) keeps jobs whose upper figure is at least $190k. It does
**not** hide postings that state no salary - measured at 2328 results with the filter alone
versus 1572 with "Transparent salaries only" also enabled, so 756 undisclosed postings passed
through. That matters, because postings without a stated salary are a large and *good* slice of
the results rather than noise to discard.

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

#### Stage 2: rollup

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

#### Stage 3: filter

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

#### Stage 4: render

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

### Job shortlist

Where company discovery asks *which companies near me employ engineers*, this asks *what was
posted that I should apply to*. The unit is the posting, filters are tight, and the run is
scheduled rather than occasional.

```bash
# Stage 1: fetch raw job records for every configured shortlist search
uv run hiringcafe-toolkit job-shortlist scrape

# Smoke run before committing to a full one
uv run hiringcafe-toolkit job-shortlist scrape --max-pages 3 --no-compress
```

Output goes to `data/job_shortlist/raw/`, in the same shape as company discovery except that the
records file is gzipped by default:

| File | Contents |
|---|---|
| `jobs-<timestamp>.jsonl.gz` | One raw record per line, deduplicated by `objectID` across every search, otherwise untouched |
| `meta-<timestamp>.json` | One entry per search: searchState, per-page counts, build ids, reported totals, overlap with earlier searches, stop reason |

**Several searches, one file.** `[search].searchstate_paths` lists searches scraped in order
into a single raw file - currently the local 30-mile search and a US-wide remote one. One file
per run keeps every later stage reading a single newest input, and a remote job near home that
both searches find is written once, so the report and the seen store treat it as one posting.
Two details keep this honest. The page ceiling applies to each search separately. And the stop
rule ("two consecutive pages with nothing new") counts records new to *that search*, not to
the file: the remote search opens with pages of nearby remote jobs the local search already
wrote, and counting those as stale would end it after two pages. Each search's
`already_written` in the meta is that overlap.

**On the fetch window.** Keep `dateFetchedPastNDays` at 21. The window is not a freshness filter -
freshness comes from comparing against postings already seen. It is a *missed-run recovery
buffer*: at 21 days you can skip three weeks of runs and lose nothing, whereas a 2-day window
turns one skipped day into postings you can never retrieve. Three weeks of slack costs about a
minute of scraping, so buy it. The scrape prints the window it used for exactly this reason.

#### Stage 2: normalize

```bash
uv run hiringcafe-toolkit job-shortlist normalize
```

Flattens raw records onto a schema this pipeline controls and collapses duplicate listings,
writing to `data/job_shortlist/interim/`:

| File | Contents |
|---|---|
| `postings-<timestamp>.jsonl.gz` | One posting per line on the flat `Posting` schema, newest first |
| `normalize-meta-<timestamp>.json` | Merge threshold and how it was derived, cluster-key coverage, merge and decline counts |

**Why flatten.** Raw records nest their useful fields under `v5_processed_job_data` and
`enriched_company_data`. Screening rules that reach into those paths break when the vendor
renames a field, and they break *quietly*: a renamed field reads as absent, an absent field
fails a check, and postings disappear with no error. Projecting onto a flat schema means a
rename breaks one function loudly instead.

**Why duplicate collapsing needs two stages.** `liberal_dedup_cluster` is hiring.cafe's own
cross-ATS duplicate key and is trusted wherever it appears, but it is present on only about a
third of records. The fallback key `(company, title, cities)` does the rest of the work, and on
its own it over-collapses: large employers post many distinct roles under one generic title, and
merging them means the second is never seen. So the key only proposes candidates, and a
similarity check on the requirements text confirms each merge before it happens.

Source is deliberately *not* part of the fallback key. A true cross-ATS duplicate appears under
different sources by definition, so including it would block exactly the merges the key exists
to make.

**Why the raw title matters.** `core_job_title` has seniority stripped upstream - 208 raw titles
in a 400-record sample carry a level marker against only 32 core titles. So four requisitions at
levels I, 1.5 and II arrive with the same core title, the same company, the same city, and
near-identical boilerplate requirements, and text similarity cannot separate them. A level
signature read from the posting's own title is what keeps them apart. An absent level counts as
a level of its own, because "Software Engineer" and "Senior Software Engineer" are different
jobs. The guard applies only to the fallback path; a cluster key stays authoritative.

**Why the threshold is calibrated per run.** Records that carry a cluster key are known
duplicates, so their text-similarity distribution is what a real duplicate looks like in this
data - measured around 0.66 on average, with the weak tail near 0.44. A hardcoded constant would
reject merges the API itself makes, because the same job gets rewritten for each ATS and rarely
scores near 1.0. The threshold and the number of ground-truth groups behind it are both recorded
in the meta, so a run that fell back to the default is visible rather than silent. Override with
`--similarity` when experimenting.

Erring toward under-merging is deliberate: a missed merge shows the same job twice and costs one
glance, while a wrong merge deletes a job you never see. The `fallback_merges_declined` count is
how often that judgment was exercised.

#### Stage 3: screen

```bash
uv run hiringcafe-toolkit job-shortlist screen
uv run hiringcafe-toolkit job-shortlist screen --comp-floor 200000   # try a threshold
```

Bands every posting, writing all three bands to `data/job_shortlist/processed/`:

| File | Contents |
|---|---|
| `screened-<timestamp>.jsonl.gz` | Every posting with `band`, `reject_reasons`, and `demote_reasons`, strong first |
| `screen-meta-<timestamp>.json` | Settings used, band counts, reason counts, near-miss count |

**Three verdicts, not two.** The available signals fall into two very different
classes. *Hard rejects* read facts - an expired flag, a commitment type, a government
domain, a stated salary - and being strict there is safe because the input is not in
doubt. *Demotions* read judgement calls, like whether a title describes building systems
or selling them, and a match moves a posting out of `strong` without deleting it.

Two fields earned demotion by measurement rather than caution. `role_type` labels about a
quarter of its People Manager postings as management on nothing but an IC ladder title
like "Principal Engineer", so it only rejects when a management title or management
language corroborates it. `seniority_level` disagrees with the level written in the
posting's own title about one time in seven, in both directions, so the raw title's level
marker is read alongside it.

Frequent air travel (`air_travel_requirement` Moderate or Extensive) demotes. It marks field,
consulting, and implementation work - "remote" roles spent at client sites - and was measured
at 134 of 5991 postings: implementation consultants, forward-deployed and commissioning
engineers, field service. It demotes rather than rejects because the level is vendor-inferred.

Remote postings are screened like any other. The data encodes a remote role's scope several
ways - open US-wide, limited to listed states, or naming only cities - and measured on 669
remote postings the split was 228 / 77 / 359. What a state list or a city list means for who
may apply is not clear enough to filter on, so nothing reads those fields.

**Every applicable reason is recorded**, not the first to fire. Stopping at the first
match makes "would have been strong except for compensation" unanswerable, and that is
the set worth re-reading whenever a threshold moves. The run prints its size.

**Rejected postings are written out too.** A filter whose discards are invisible cannot
be checked, and the discards here are large - compensation alone is about half of them.
To read them:

```bash
zcat data/job_shortlist/processed/screened-*.jsonl.gz \
  | jq -r 'select(.reject_reasons == ["comp below floor"]) | "\(.comp_max)\t\(.raw_title)\t\(.company)"'
```

#### Stage 4: diff

```bash
uv run hiringcafe-toolkit job-shortlist diff
uv run hiringcafe-toolkit job-shortlist diff --visit-log data/inputs/visits.json
uv run hiringcafe-toolkit job-shortlist diff --dry-run
```

Drops postings a previous run already surfaced, writing to
`data/job_shortlist/processed/` and updating `data/job_shortlist/state/seen.jsonl`:

| File | Contents |
|---|---|
| `shortlist-<timestamp>.jsonl.gz` | Postings to render: new since the last run, plus any promoted band |
| `diff-meta-<timestamp>.json` | New, promoted, suppressed and store counts |
| `state/seen.jsonl` | One line per posting ever surfaced. **Not regenerable** |

**Two application labels, kept apart.** `applied` means a resume went in for *that
posting*, set by F9 on the posting's own tab. `company_applied` means a resume went to that
*employer*, set by F9 on their page or recorded from any earlier visit. Browsing a
company's own listings and applying to something found there sets the second and not the
first, because the role applied to need not be the one that surfaced. Merging them would
make it impossible to tell whether the screen picked the right posting or merely the right
company - which is the distinction the labels exist to measure.

Employer hosts are matched by domain rather than exact string: the log records whichever
host was opened, often a careers subdomain like `careers.appian.com`, while the posting
carries the registrable domain from the company's homepage.

**How identity is decided.** A posting is recognized by the ids of every listing merged
into it, plus the vendor's cluster key. Which listing survives a merge depends on source
preference and publish date, so tomorrow's run can pick a different representative for
the same job - keeping every id is what stops that looking new.

Content-derived keys were tried and rejected. A key of company, title and level collides
across genuinely distinct postings: on 116 surfaced postings it merged Capital One's
"Senior Lead Software Engineer, Front End Web" with its "Sr. Lead Software Engineer -
Back End", and Exiger's "Tech Lead/Principal Engineer" with its "Database Engineer". The
normalize stage separates those with a text-similarity check that a bare key cannot
replicate, so matching on content here would undo the stage before it.

The asymmetry decides it: failing to suppress a repost costs one glance, while
suppressing a distinct posting removes it from every future render and nothing says so.

**Suppression is seen-only, and independent of the browser.** The store records what the
pipeline surfaced; VisitLogger records what was opened. Keeping them separate means a
browser-side gap can never hide a posting - the visit log only attaches `opened` and
`applied` labels, so skipping it costs data for later analysis and nothing else.

Rendered pages are never overwritten, so a backlog stays readable in the file it was
first rendered into. A posting is re-surfaced only when its band improves, which is what
lets a rule change resurrect something previously shown as `possible`.

**Rejected postings are never stored.** They were never shown, so a rule change that
promotes one surfaces it on the next run with no special handling.

**The store is the one file worth backing up.** Everything else in `data/` regenerates
from the raw scrape in seconds; this does not, and losing it re-surfaces every posting
with no warning. It is gitignored like the rest of `data/`, and it stays small - about
390 bytes per posting, so roughly 4 MB after a year of daily runs.

#### Stage 5: render

```bash
uv run hiringcafe-toolkit job-shortlist render
uv run hiringcafe-toolkit job-shortlist render --band strong
```

Writes `shortlist-<timestamp>.html` beside the shortlist it was built from, ready for the
VisitLogger tab queue.

**Two queued links per row, employer first.** The queue is opt-in: only
`a[data-visit-open]` anchors are queued when any are present.

```html
<a href="https://acme.com" data-visit-open
   data-visit-key="acme.com" data-visit-mark="auto">Acme</a>
<a href="https://boards.greenhouse.io/acme/jobs/123" data-visit-open
   data-visit-key="job:grnhse___acme___123" data-visit-mark="auto">apply</a>
```

The employer's page opens first, because an interesting posting is a reason to look at the
company before the role. The second time that company appears the extension skips it on
its own - it refuses any key that already has a date - so a company with four postings
opens once. At five tabs per press that is roughly two or three rows per batch.

That first link goes to a careers page only when the careers page sits on the employer's
own domain; otherwise it goes to their homepage. A derived careers URL usually points at
an ATS board, and a board lists only what was posted through that one instance - Cognizant
runs three, Merck two, Accenture two - so no single board is the employer's full listing.
On measured data that means about 11% land on a careers page and 85% on a homepage, one
click from the jobs. The remaining 4% have no known homepage and get no employer link at
all, since the only candidate left would be an ATS board and the extension will not record
against one.

The keys differ deliberately. The posting is keyed `job:<id>`, because apply URLs sit on
ATS domains shared by thousands of employers and the prefix keeps the two pipelines apart
in a shared export. The employer's page is keyed by company host - the same key company
discovery uses - so **processing a company here counts as processing it there**. Both
pipelines render the same URL under that key; if they disagreed, whichever ran first would
silently decide what "processed" meant and the other page would never open.

The coupling is one-directional: a visited company never suppresses its postings, since
those are keyed separately.

Marking produces the `opened` and `applied` labels the diff stage reads back. Nothing in
the pipeline depends on them, but they can only be collected going forward, so the link is
marked anyway. `applied` needs a deliberate F9 and is the clean label; `opened` fires on
tab open.

**Both bands in one page, strong first.** The queue walks the DOM in order and skips
marked entries, so a single page works strong-first by itself and keeps its place without
you tracking which file you were in. `--band strong` renders a subset when the possible
section is too long to face.

**Pages are never overwritten.** Each run writes its own stamped file, so an unread
backlog stays readable where it was first written.

Columns carry the qualifications that make a number readable rather than misleading:
compensation is flagged `hourly` when annualized from an hourly rate, and `N metros` when
the posting spans enough cities that its ceiling belongs to the priciest one. Demote
reasons are shown, since why a posting is only `possible` is the judgement being made when
skimming. Companies you have already sent a resume to are marked - shown rather than
filtered, because two genuinely distinct roles at one employer can both be worth applying
to. That reads the visit log configured under `[visit_logger]`, shared with the
company-discovery pipeline; `--visit-log` overrides it.

#### Generating a shortlist: the full checklist

The loop is circular. Working through one page produces the visit-log marks that the next
run reads, so the checklist starts by finishing the previous cycle rather than by scraping.
On a first run, skip to step 3.

**1. Work through the previous page.** Start the tab queue from the extension's menu action.
It opens five tabs at a time, refilling as you close them, and skips anything already
marked. Each row opens the employer's page first, then the posting.

**2. Press F9 on whatever you applied through, before closing its tab.** That sets the `R`
badge and is the only source of an application label. F9 on the posting's tab records
`applied`; F9 on the employer's tab records `company_applied`, which is the one to use when
you browsed their listings and applied to something found there. It works because the tab was opened
from the queue: F9 acts on the link's key rather than the tab's own host, so the mark lands
on the posting even though the tab shows an ATS. A posting opened by hand - middle-clicked,
or from anywhere but the queue - has no such key, and F9 will be refused for the vendor
host. Queue-open or no label.

**3. Export the visit log** from the extension to the path in `[visit_logger].export_path`.
This has to happen *before* the run, not after: `diff` reads it for the `opened` and
`applied` labels, and `render` reads it to mark employers you have already applied to.

**4. Run the pipeline.**

```bash
make shortlist
```

which is the five stages in order:

```bash
uv run hiringcafe-toolkit job-shortlist scrape
uv run hiringcafe-toolkit job-shortlist normalize
uv run hiringcafe-toolkit job-shortlist screen
uv run hiringcafe-toolkit job-shortlist diff
uv run hiringcafe-toolkit job-shortlist render
```

Chain them with `make` or `&&`, never `;`. Every stage defaults to the newest file from the
one before, so a failed scrape would otherwise have the rest quietly reprocess yesterday's
data and report a shortlist of zero - which reads as a quiet day rather than a broken run.

**5. Check three numbers in the output before trusting the page.**

- `scrape` - a `stop_reason` of `reached max_pages` means the run was cut short, not that
  the result set ended.
- `diff` - `suppressed` should be most of the screened postings on a normal day. Near zero
  means the store is not being found; near everything means nothing new arrived.
- `diff` - `opened` should be non-zero once you have worked a page. Zero after a session
  means the queue marks are not reaching the store, and that session's labels are gone.
  `applied to posting` and `applied at company` can each legitimately be zero, depending on
  whether you applied from the posting or from the employer's own listings.

**6. Open the new page** in `data/job_shortlist/processed/`, and go back to step 1 next time.

Two things that make skipping safe. The 21-day fetch window is three weeks of missed-run
slack, so a skipped week costs nothing. And rendered pages are never overwritten, so an
unread backlog waits in the file it was first written to - use `--band strong` to generate
a shorter page when the `possible` section is too long to face.

#### When the scraper is blocked: capturing in the browser

Some days hiring.cafe turns the scraper away with a Cloudflare challenge. `make shortlist` then
stops at the scrape with *"hiring.cafe is answering the scraper with a Cloudflare challenge"*
(exit code 3) and prints the routine below. On those days you run the searches yourself in
Firefox, and a small extension saves the result pages as you page through them.

**Quick start**

1. **Load the extension** (once per Firefox session). Open `about:debugging`, choose
   **This Firefox**, then **Load Temporary Add-on…**, and pick
   `extensions/hiringcafe-capture/manifest.json`. Its blue page icon lives under the
   puzzle-piece **Extensions** menu; to keep it in view, click the gear next to it and choose
   **Pin to Toolbar**.
2. **Start clean.** Click the blue icon, then **Clear**, then **Click again to clear**.
3. **Open the searches.** Run `make urls`. It prints two sets of links:
   - **Daily** - postings from about the last two days. Use these when the last run was
     yesterday; they are a fraction of the pages.
   - **Catch-up** - the full 21 days. Use these after missed days.

   Open one link per search (both searches, from the same set).
4. **Page through each search** by pressing **Alt+N**, which clicks the site's "Next page" link
   for you - one page per press. Keep going until the extension's popup shows the search as
   **Complete**; the icon's badge turns green once every search is.
5. **Save.** Click the blue icon, then **Save**. The capture lands in
   `~/Downloads/hiringcafe-capture/`.
6. **Build the shortlist** from it:

   ```bash
   make shortlist-import
   ```

   Read any yellow warnings it prints before trusting the page (see below).

Temporary add-ons disappear when Firefox restarts, along with any pages not yet saved - so Save
before closing Firefox. Alt+N can be changed in `about:addons` -> gear -> **Manage Extension
Shortcuts**.

**What the extension does, and does not do.** It only watches. Every page request comes from
you - a click or one Alt+N press - and the extension never pages, clicks, or requests anything
on its own; that would make it an automated client using your session to get past a check the
site applies on purpose, which is also why the scraper never tries to get past the challenge
itself. It saves each result page whole - the first page from the HTML, later ones from the
`classic.json` requests "Next page" makes - tagged with the searchState and page number from
its URL, and notes when a search's "Next page" link disappears, which is how it knows the last
page was reached. Finding the postings in a page is left to the same Python code the live
client uses.

**What `make shortlist-import` checks.** `job-shortlist import` writes the same
`jobs-*.jsonl.gz` and meta a scrape writes, from the newest capture, then the usual four stages
run. Each check below catches something that would otherwise produce a run that looks
complete:

- **Every configured search must be in the capture.** A missing one stops the import; the diff
  would otherwise report nothing new for it.
- **A capture matches a configured search only if their searchStates are identical apart from
  the fetch window**, so a daily capture counts for its search. Anything else browsed with the
  extension loaded is skipped with a warning naming it.
- **"Last page not reached"** - the search was not paged to the end, so later postings are
  missing. Page on and save again.
- **"Missing pages"** - a page in the middle was not captured, usually one the browser served
  from its cache. Reload it and save again.
- **"Postings fetched in between may be missed"** - the previous successful run is older than
  the capture's window, so a daily capture does not reach back to it. Capture again with the
  Catch-up links.
- **Pages captured over more than six hours** are flagged as a likely uncleared earlier session.

Every capture's details - window, pages, gaps, completeness - are recorded in the meta under
`captures`. The site's reported total is not a completeness check here or anywhere: it counts
duplicate listings, measured at 4326 reported against 2347 unique records on one run.

**If something goes wrong.** In `about:debugging`, click **Inspect** next to the extension
and look at its **Console**. A popup that stays empty while you page means the pages are not
reaching the extension; Alt+N doing nothing on a search page usually means the page had not
finished loading - a short message in the page's corner says which.

**On compression.** Raw records gzip about 7x, since they are mostly repeated JSON keys. Company
discovery runs occasionally and leaves its output plain; this pipeline runs daily and keeps every
snapshot, which reaches several GB within a year uncompressed. Reading accepts both forms
unconditionally, so `--no-compress` and existing plain files both keep working.

## License

MIT. See [LICENSE](LICENSE).
