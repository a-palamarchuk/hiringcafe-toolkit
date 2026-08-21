"""Command-line entry point for hiringcafe-toolkit.

Each pipeline is a Typer sub-app and each stage is its own command, so stages
can be run, re-run, and scheduled independently. Re-running rollup after
changing a filter should never mean re-scraping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from hiringcafe_toolkit.api import HiringCafeClient
from hiringcafe_toolkit.common.config import (
    ConfigError,
    load_company_discovery_config,
    load_search_state,
)
from hiringcafe_toolkit.common.jsonl import read_jsonl
from hiringcafe_toolkit.company_discovery.rollup import RollupOptions, run_rollup
from hiringcafe_toolkit.company_discovery.scrape import run_scrape
from hiringcafe_toolkit.company_discovery.visited_filter import (
    VisitLogError,
    load_visit_log,
    run_filter,
)

DEFAULT_CONFIG_PATH = Path("config/company_discovery.toml")
DEFAULT_RAW_DIR = Path("data/company_discovery/raw")
DEFAULT_INTERIM_DIR = Path("data/company_discovery/interim")

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
        )

    typer.echo("")
    typer.echo(f"Records:  {result.jobs_path}")
    typer.echo(f"Meta:     {result.meta_path}")
    typer.echo(f"Unique:   {result.unique_records} over {result.pages_fetched} pages")
    typer.echo(f"Stopped:  {result.stop_reason}")
    for key, value in sorted(result.reported_totals.items()):
        typer.echo(f"Reported: {key} = {value:g}")


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

    jobs_path = jobs if jobs is not None else _newest(raw_dir, "jobs-*.jsonl")
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


if __name__ == "__main__":
    app()
