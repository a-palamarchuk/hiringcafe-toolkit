"""Command-line entry point for hiringcafe-toolkit.

Each pipeline is a Typer sub-app and each stage is its own command, so stages
can be run, re-run, and scheduled independently. Re-running rollup after
changing a filter should never mean re-scraping.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from hiringcafe_toolkit.api import HiringCafeClient
from hiringcafe_toolkit.common.config import (
    ConfigError,
    load_company_discovery_config,
    load_job_shortlist_config,
    load_search_state,
)
from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.common.scrape import ScrapeResult, run_scrape
from hiringcafe_toolkit.company_discovery.render import run_render
from hiringcafe_toolkit.company_discovery.rollup import RollupOptions, run_rollup
from hiringcafe_toolkit.company_discovery.visited_filter import (
    VisitLogError,
    load_visit_log,
    run_filter,
)
from hiringcafe_toolkit.job_shortlist.diff import run_diff
from hiringcafe_toolkit.job_shortlist.normalize import run_normalize
from hiringcafe_toolkit.job_shortlist.render import BAND_ORDER
from hiringcafe_toolkit.job_shortlist.render import run_render as run_shortlist_render
from hiringcafe_toolkit.job_shortlist.screen import run_screen

DEFAULT_CONFIG_PATH = Path("config/company_discovery.toml")
DEFAULT_RAW_DIR = Path("data/company_discovery/raw")
DEFAULT_INTERIM_DIR = Path("data/company_discovery/interim")
DEFAULT_PROCESSED_DIR = Path("data/company_discovery/processed")

SHORTLIST_CONFIG_PATH = Path("config/job_shortlist.toml")
SHORTLIST_RAW_DIR = Path("data/job_shortlist/raw")
SHORTLIST_INTERIM_DIR = Path("data/job_shortlist/interim")
SHORTLIST_PROCESSED_DIR = Path("data/job_shortlist/processed")

POSTINGS_GLOB = "postings-*.jsonl*"
SCREENED_GLOB = "screened-*.jsonl*"

SHORTLIST_STATE_PATH = Path("data/job_shortlist/state/seen.jsonl")
SHORTLIST_GLOB = "shortlist-*.jsonl*"

#: Raw scrape output may be gzipped or not; stages accept either, so lookups
#: glob both rather than assuming whichever the last run happened to write.
JOBS_GLOB = "jobs-*.jsonl*"

app = typer.Typer(
    help="Personal hiring.cafe scraping and processing toolkit.", no_args_is_help=True
)

company_discovery_app = typer.Typer(
    help="Scrape -> Rollup -> Filter -> Render nearby company career pages.",
    no_args_is_help=True,
)
job_shortlist_app = typer.Typer(
    help="Daily scrape and shortlist of new job postings (NOVA, then staged US remote).",
    no_args_is_help=True,
)

app.add_typer(company_discovery_app, name="company-discovery")
app.add_typer(job_shortlist_app, name="job-shortlist")


def _newest(directory: Path, pattern: str) -> Path:
    """Most recently modified matching file, so stages chain without paths."""
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        typer.secho(
            f"No files matching {pattern} in {directory}. Run the previous stage first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    return matches[0]


def _newest_optional(directory: Path, pattern: str) -> Path | None:
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _echo_scrape_result(result: ScrapeResult) -> None:
    """Print the post-run summary. Identical for every pipeline."""
    typer.echo("")
    typer.echo(f"Records:  {result.jobs_path}")
    typer.echo(f"Meta:     {result.meta_path}")
    typer.echo(f"Unique:   {result.unique_records} over {result.pages_fetched} pages")
    typer.echo(f"Stopped:  {result.stop_reason}")
    for key, value in sorted(result.reported_totals.items()):
        typer.echo(f"Reported: {key} = {value:g}")
    if result.stop_reason.startswith("reached max_pages"):
        typer.secho(
            "  This run hit the page ceiling and did not finish. Re-run with a higher --max-pages.",
            fg=typer.colors.YELLOW,
        )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )


@company_discovery_app.command("scrape")
def company_discovery_scrape(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = DEFAULT_CONFIG_PATH,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for raw run output.")
    ] = DEFAULT_RAW_DIR,
    max_pages: Annotated[
        int | None, typer.Option("--max-pages", help="Override the config page ceiling.")
    ] = None,
    delay: Annotated[
        float | None, typer.Option("--delay", help="Override seconds between page requests.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Fetch raw job records for the configured search."""
    _configure_logging(verbose)

    try:
        settings = load_company_discovery_config(config)
        search_state = load_search_state(settings.searchstate_path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    effective_delay = delay if delay is not None else settings.scrape.delay_seconds
    effective_max_pages = max_pages if max_pages is not None else settings.scrape.max_pages

    typer.echo(f"searchState: {settings.searchstate_path}")
    with HiringCafeClient(delay_seconds=effective_delay) as client:
        result = run_scrape(
            search_state,
            out_dir,
            client=client,
            max_pages=effective_max_pages,
            variant_name=settings.searchstate_path.stem,
            compress=settings.scrape.compress,
        )

    _echo_scrape_result(result)


@company_discovery_app.command("rollup")
def company_discovery_rollup(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = DEFAULT_CONFIG_PATH,
    jobs: Annotated[
        Path | None,
        typer.Option("--jobs", "-j", help="Raw JSONL to roll up. Defaults to the newest."),
    ] = None,
    raw_dir: Annotated[
        Path, typer.Option("--raw-dir", help="Where to look for raw scrape output.")
    ] = DEFAULT_RAW_DIR,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for rolled-up output.")
    ] = DEFAULT_INTERIM_DIR,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Aggregate raw postings into one record per company."""
    _configure_logging(verbose)

    try:
        settings = load_company_discovery_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    jobs_path = jobs if jobs is not None else _newest(raw_dir, JOBS_GLOB)
    typer.echo(f"Source: {jobs_path}")

    options = RollupOptions(
        home_latitude=settings.home.latitude,
        home_longitude=settings.home.longitude,
        radius_miles=settings.rollup.radius_miles,
        excluded_states=settings.rollup.excluded_states,
        excluded_sources=settings.rollup.excluded_sources,
        excluded_website_tlds=settings.rollup.excluded_website_tlds,
    )
    result = run_rollup(read_jsonl(jobs_path), out_dir, options, source_path=jobs_path)

    typer.echo("")
    typer.echo(f"Companies:   {result.companies_path} ({result.company_count})")
    typer.echo(f"No website:  {result.tail_path} ({result.tail_count})")
    typer.echo(f"Meta:        {result.meta_path}")
    typer.echo("")
    for key in sorted(result.stats):
        typer.echo(f"  {key}: {result.stats[key]}")


@company_discovery_app.command("filter")
def company_discovery_filter(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = DEFAULT_CONFIG_PATH,
    companies: Annotated[
        Path | None,
        typer.Option("--companies", help="Rolled-up JSONL to filter. Defaults to the newest."),
    ] = None,
    visit_log: Annotated[
        Path | None,
        typer.Option("--visit-log", help="VisitLogger export. Defaults to the configured path."),
    ] = None,
    interim_dir: Annotated[
        Path, typer.Option("--interim-dir", help="Where to look for rolled-up output.")
    ] = DEFAULT_INTERIM_DIR,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for filtered output.")
    ] = DEFAULT_INTERIM_DIR,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Drop companies whose site is already in the visit log."""
    _configure_logging(verbose)

    try:
        settings = load_company_discovery_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    visit_log_path = visit_log if visit_log is not None else settings.visit_log_path
    if visit_log_path is None:
        typer.secho(
            "No visit log. Set [visit_logger].export_path in the config or pass --visit-log.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    companies_path = (
        companies if companies is not None else _newest(interim_dir, "companies-2*.jsonl")
    )
    tail_path = _newest_optional(interim_dir, "companies-no-website-*.jsonl")

    try:
        log = load_visit_log(visit_log_path)
    except VisitLogError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"Companies: {companies_path}")
    typer.echo(f"Visit log: {visit_log_path} ({len(log.hosts)} hosts)")
    if log.unusable_keys:
        typer.secho(
            f"  {len(log.unusable_keys)} visit-log keys were not usable hosts",
            fg=typer.colors.YELLOW,
        )

    result = run_filter(
        read_jsonl(companies_path),
        log,
        out_dir,
        tail=read_jsonl(tail_path) if tail_path else None,
        source_path=companies_path,
        visit_log_path=visit_log_path,
    )

    typer.echo("")
    typer.echo(f"Remaining:  {result.remaining_path} ({result.kept})")
    typer.echo(f"No website: {result.passthrough} passed through unfiltered")
    typer.echo(f"Meta:       {result.meta_path}")
    typer.echo(f"Excluded as already visited: {result.excluded}")


@company_discovery_app.command("render")
def company_discovery_render(
    companies: Annotated[
        Path | None,
        typer.Option("--companies", help="Filtered JSONL to render. Defaults to the newest."),
    ] = None,
    interim_dir: Annotated[
        Path, typer.Option("--interim-dir", help="Where to look for filtered output.")
    ] = DEFAULT_INTERIM_DIR,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for the rendered pages.")
    ] = DEFAULT_PROCESSED_DIR,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Render the visit list as HTML for the VisitLogger tab queue."""
    _configure_logging(verbose)

    companies_path = (
        companies if companies is not None else _newest(interim_dir, "remaining-2*.jsonl")
    )
    tail_path = _newest_optional(interim_dir, "remaining-no-website-*.jsonl")

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    main_result = run_render(
        read_jsonl(companies_path),
        out_dir / f"companies-{stamp}.html",
        title="Companies to visit",
        note=(
            "One queued link per row (careers), keyed to the company domain. "
            "Company and search links are not queued."
        ),
    )
    typer.echo(f"Source:     {companies_path}")
    typer.echo(f"Companies:  {main_result.path} ({main_result.rows} rows)")

    if tail_path is not None:
        tail_result = run_render(
            read_jsonl(tail_path),
            out_dir / f"companies-no-website-{stamp}.html",
            title="Companies without a website",
            note=(
                "These rows have no company domain, so nothing is marked visited and the "
                "list repeats in full on every run. When you find the real company site, "
                "mark it there."
            ),
        )
        typer.echo(f"No website: {tail_result.path} ({tail_result.rows} rows)")


@job_shortlist_app.command("scrape")
def job_shortlist_scrape(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = SHORTLIST_CONFIG_PATH,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for raw run output.")
    ] = SHORTLIST_RAW_DIR,
    max_pages: Annotated[
        int | None, typer.Option("--max-pages", help="Override the config page ceiling.")
    ] = None,
    delay: Annotated[
        float | None, typer.Option("--delay", help="Override seconds between page requests.")
    ] = None,
    no_compress: Annotated[
        bool, typer.Option("--no-compress", help="Write raw records uncompressed.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Fetch raw job records for the configured shortlist search."""
    _configure_logging(verbose)

    try:
        settings = load_job_shortlist_config(config)
        search_state = load_search_state(settings.searchstate_path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    effective_delay = delay if delay is not None else settings.scrape.delay_seconds
    effective_max_pages = max_pages if max_pages is not None else settings.scrape.max_pages
    compress = settings.scrape.compress and not no_compress

    window = search_state.get("dateFetchedPastNDays")
    typer.echo(f"searchState: {settings.searchstate_path}")
    if isinstance(window, int):
        # Surfaced because it is the parameter most likely to be wrong after a
        # searchState is re-copied from the UI, and a too-narrow window loses
        # postings that cannot be recovered on a later run.
        typer.echo(f"Fetch window: {window} days")

    with HiringCafeClient(delay_seconds=effective_delay) as client:
        result = run_scrape(
            search_state,
            out_dir,
            client=client,
            max_pages=effective_max_pages,
            variant_name=settings.searchstate_path.stem,
            compress=compress,
        )

    _echo_scrape_result(result)


@job_shortlist_app.command("normalize")
def job_shortlist_normalize(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = SHORTLIST_CONFIG_PATH,
    jobs: Annotated[
        Path | None,
        typer.Option("--jobs", "-j", help="Raw JSONL to normalize. Defaults to the newest."),
    ] = None,
    raw_dir: Annotated[
        Path, typer.Option("--raw-dir", help="Where to look for raw scrape output.")
    ] = SHORTLIST_RAW_DIR,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for normalized output.")
    ] = SHORTLIST_INTERIM_DIR,
    similarity: Annotated[
        float | None,
        typer.Option(
            "--similarity",
            help="Override the merge threshold. Default calibrates from the run's own "
            "cluster-keyed records.",
        ),
    ] = None,
    no_compress: Annotated[
        bool, typer.Option("--no-compress", help="Write postings uncompressed.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Flatten raw records and collapse duplicate listings."""
    _configure_logging(verbose)

    try:
        settings = load_job_shortlist_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    jobs_path = jobs if jobs is not None else _newest(raw_dir, JOBS_GLOB)
    typer.echo(f"Source: {jobs_path}")

    result = run_normalize(
        read_jsonl(jobs_path),
        out_dir,
        source_path=jobs_path,
        compress=settings.scrape.compress and not no_compress,
        threshold=similarity,
    )

    typer.echo("")
    typer.echo(f"Postings: {result.postings_path} ({result.postings_out})")
    typer.echo(f"Meta:     {result.meta_path}")
    typer.echo(f"Collapsed {result.postings_in} listings into {result.postings_out}")
    typer.echo(f"Merge threshold: {result.threshold:.2f}")
    typer.echo("")
    for key in sorted(result.stats):
        typer.echo(f"  {key}: {result.stats[key]}")
    declined = result.stats.get("fallback_merges_declined", 0)
    if declined:
        typer.echo("")
        typer.secho(
            f"  {declined} candidate merges declined on text disagreement - these are "
            "distinct roles sharing a title.",
            fg=typer.colors.CYAN,
        )


@job_shortlist_app.command("screen")
def job_shortlist_screen(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = SHORTLIST_CONFIG_PATH,
    postings: Annotated[
        Path | None,
        typer.Option("--postings", "-p", help="Normalized JSONL. Defaults to the newest."),
    ] = None,
    interim_dir: Annotated[
        Path, typer.Option("--interim-dir", help="Where to look for normalized output.")
    ] = SHORTLIST_INTERIM_DIR,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for screened output.")
    ] = SHORTLIST_PROCESSED_DIR,
    comp_floor: Annotated[
        int | None, typer.Option("--comp-floor", help="Override the compensation floor.")
    ] = None,
    no_compress: Annotated[
        bool, typer.Option("--no-compress", help="Write screened output uncompressed.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Band normalized postings as strong, possible, or rejected."""
    _configure_logging(verbose)

    try:
        settings = load_job_shortlist_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    screen_settings = settings.screen
    if comp_floor is not None:
        screen_settings = replace(screen_settings, comp_floor=comp_floor)

    postings_path = postings if postings is not None else _newest(interim_dir, POSTINGS_GLOB)
    typer.echo(f"Source: {postings_path}")
    typer.echo(f"Comp floor: ${screen_settings.comp_floor:,}")

    result = run_screen(
        read_jsonl(postings_path),
        out_dir,
        screen_settings,
        source_path=postings_path,
        compress=settings.scrape.compress and not no_compress,
    )

    total = sum(result.counts.values())
    typer.echo("")
    typer.echo(f"Screened: {result.screened_path}")
    typer.echo(f"Meta:     {result.meta_path}")
    typer.echo("")
    for band in ("strong", "possible", "rejected"):
        count = result.counts.get(band, 0)
        typer.echo(f"  {band:10s} {count:6d}   {count / max(total, 1) * 100:5.1f}%")
    typer.echo("")
    typer.echo("Rejected for (a posting may have several):")
    for reason, count in result.reject_reasons.items():
        typer.echo(f"  {reason:34s} {count:6d}")
    if result.demote_reasons:
        typer.echo("")
        typer.echo("Demoted for:")
        for reason, count in result.demote_reasons.items():
            typer.echo(f"  {reason:34s} {count:6d}")
    if result.near_misses:
        typer.echo("")
        typer.secho(
            f"  {result.near_misses} postings would be strong but for compensation. "
            f"Grep the output for them before moving the floor.",
            fg=typer.colors.CYAN,
        )


@job_shortlist_app.command("diff")
def job_shortlist_diff(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = SHORTLIST_CONFIG_PATH,
    screened: Annotated[
        Path | None,
        typer.Option("--screened", "-s", help="Screened JSONL. Defaults to the newest."),
    ] = None,
    processed_dir: Annotated[
        Path, typer.Option("--processed-dir", help="Where to look for screened output.")
    ] = SHORTLIST_PROCESSED_DIR,
    out_dir: Annotated[
        Path, typer.Option("--out-dir", "-o", help="Directory for the shortlist.")
    ] = SHORTLIST_PROCESSED_DIR,
    store: Annotated[
        Path, typer.Option("--store", help="Seen-postings store.")
    ] = SHORTLIST_STATE_PATH,
    visit_log: Annotated[
        Path | None,
        typer.Option(
            "--visit-log",
            help="VisitLogger export. Defaults to visit_logger.export_path in the config.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report without updating the store.")
    ] = False,
    no_compress: Annotated[
        bool, typer.Option("--no-compress", help="Write the shortlist uncompressed.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Drop postings already surfaced by a previous run."""
    _configure_logging(verbose)

    try:
        settings = load_job_shortlist_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    log_path = visit_log if visit_log is not None else settings.visit_log_path
    screened_path = screened if screened is not None else _newest(processed_dir, SCREENED_GLOB)
    typer.echo(f"Source: {screened_path}")
    typer.echo(f"Store:  {store}")

    result = run_diff(
        read_jsonl(screened_path),
        out_dir,
        store,
        visit_log_path=log_path,
        source_path=screened_path,
        compress=settings.scrape.compress and not no_compress,
        dry_run=dry_run,
    )

    typer.echo("")
    typer.echo(f"Shortlist: {result.shortlist_path} ({result.surfaced})")
    typer.echo(f"Meta:      {result.meta_path}")
    typer.echo("")
    typer.echo(f"  new                {result.new_postings:6d}")
    typer.echo(f"  promoted           {result.promoted:6d}")
    typer.echo(f"  suppressed         {result.suppressed:6d}")
    for band in ("strong", "possible"):
        typer.echo(f"  {band:18s} {result.bands.get(band, 0):6d}")
    typer.echo(f"  store entries      {result.store_size:6d}")
    if log_path:
        typer.echo(f"  opened             {result.opened:6d}")
        typer.echo(f"  applied to posting {result.applied:6d}")
        typer.echo(f"  applied at company {result.company_applied:6d}")
    else:
        typer.secho(
            "\n  No visit log configured or given, so no opened/applied labels were "
            "recorded. Those can only be collected going forward.",
            fg=typer.colors.YELLOW,
        )
    if dry_run:
        typer.secho("\n  Dry run: the store was not updated.", fg=typer.colors.CYAN)


@job_shortlist_app.command("render")
def job_shortlist_render(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Pipeline TOML config.")
    ] = SHORTLIST_CONFIG_PATH,
    shortlist: Annotated[
        Path | None,
        typer.Option("--shortlist", "-s", help="Shortlist JSONL. Defaults to the newest."),
    ] = None,
    processed_dir: Annotated[
        Path, typer.Option("--processed-dir", help="Where to look for the shortlist.")
    ] = SHORTLIST_PROCESSED_DIR,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Output HTML. Defaults to a stamped name."),
    ] = None,
    band: Annotated[
        list[str] | None,
        typer.Option("--band", help="Bands to include. Repeatable. Default: both."),
    ] = None,
    visit_log: Annotated[
        Path | None,
        typer.Option(
            "--visit-log",
            help="VisitLogger export. Defaults to visit_logger.export_path in the config.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Render the shortlist as a VisitLogger-compatible page."""
    _configure_logging(verbose)

    try:
        settings = load_job_shortlist_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    log_path = visit_log if visit_log is not None else settings.visit_log_path
    shortlist_path = shortlist if shortlist is not None else _newest(processed_dir, SHORTLIST_GLOB)
    stamp = shortlist_path.name.removeprefix("shortlist-").split(".")[0]
    out_path = out if out is not None else processed_dir / f"shortlist-{stamp}.html"

    applied: frozenset[str] = frozenset()
    if log_path is not None:
        try:
            applied = load_visit_log(log_path).applied_hosts
        except VisitLogError as exc:
            # The export is made by hand, so a missing one is a normal state.
            # The markers are informational; losing them must not stop a render.
            typer.secho(f"{exc} - continuing without applied markers", fg=typer.colors.YELLOW)

    result = run_shortlist_render(
        read_jsonl(shortlist_path),
        out_path,
        title="Job shortlist",
        applied_hosts=applied,
        bands=band if band else BAND_ORDER,
        note=f"Source: {shortlist_path.name}",
    )

    typer.echo(f"Source: {shortlist_path}")
    typer.echo(f"Page:   {result.path} ({result.rows} rows)")
    for name, count in result.bands.items():
        if count:
            typer.echo(f"  {name:10s} {count:6d}")
    if not result.rows:
        typer.secho(
            "  Nothing to render. Every posting was already surfaced by an earlier run; "
            "the backlog is in the previous page.",
            fg=typer.colors.CYAN,
        )


if __name__ == "__main__":
    app()
