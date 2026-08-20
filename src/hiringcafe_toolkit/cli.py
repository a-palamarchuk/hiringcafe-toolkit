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
from hiringcafe_toolkit.company_discovery.scrape import run_scrape

DEFAULT_CONFIG_PATH = Path("config/company_discovery.toml")
DEFAULT_RAW_DIR = Path("data/company_discovery/raw")

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


if __name__ == "__main__":
    app()
