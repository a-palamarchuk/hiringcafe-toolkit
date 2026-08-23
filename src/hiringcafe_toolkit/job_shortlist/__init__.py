"""Job-shortlist pipeline: Scrape -> Normalize -> Screen -> Diff -> Render.

Where company discovery asks which companies nearby employ engineers, this
asks which postings are worth applying to. The unit is the posting, filters
are tight, and runs are scheduled, so this pipeline needs its own
seen-postings state store that company discovery has no use for.

The scrape stage is shared and lives in ``common.scrape``.
"""

from hiringcafe_toolkit.job_shortlist.normalize import (
    NormalizeResult,
    Posting,
    calibrate_similarity,
    deduplicate,
    fallback_key,
    normalize_record,
    normalized_title,
    run_normalize,
    similarity,
)
from hiringcafe_toolkit.job_shortlist.screen import (
    ScreenResult,
    Verdict,
    assign_band,
    demote_reasons,
    reject_reasons,
    run_screen,
    screen,
)

__all__ = [
    "NormalizeResult",
    "Posting",
    "calibrate_similarity",
    "deduplicate",
    "fallback_key",
    "normalize_record",
    "normalized_title",
    "run_normalize",
    "ScreenResult",
    "Verdict",
    "assign_band",
    "demote_reasons",
    "reject_reasons",
    "run_screen",
    "screen",
    "similarity",
]
