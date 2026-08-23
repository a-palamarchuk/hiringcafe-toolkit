"""Stage 3 of job shortlist: reject, demote, and band.

Three verdicts rather than two, because the signals available fall into two
very different classes and collapsing them loses postings.

**Hard rejects** read facts: an expired flag, a commitment type, a government
domain, a stated salary. Being strict here is safe because the input is not in
doubt.

**Demotions** read judgement calls: whether a title describes building systems
or selling them, whether a tool list belongs to an engineer or an
administrator. These signals are useful and unreliable at the same time, so a
match moves a posting out of ``strong`` without deleting it. Two fields earned
this treatment by measurement rather than caution: ``role_type`` labels roughly
a quarter of its People Manager postings as management on nothing but an IC
ladder title like "Principal Engineer", and ``seniority_level`` disagrees with
the level written in the posting's own title about one time in seven, in both
directions.

**Every applicable reason is recorded**, not the first one to fire. Stopping at
the first match makes "would have been strong except for compensation"
unanswerable, and that set is the one worth re-reading whenever a threshold
moves.

Rejected postings are written out alongside the rest. A filter whose
discards are invisible cannot be checked, and the discards here are large:
compensation alone accounts for roughly half of them.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hiringcafe_toolkit.common.config import ScreenSettings
from hiringcafe_toolkit.common.jsonl import JsonlWriter, with_compression
from hiringcafe_toolkit.job_shortlist.normalize import Posting, level_signature

JsonDict = dict[str, Any]

BAND_STRONG = "strong"
BAND_POSSIBLE = "possible"
BAND_REJECTED = "rejected"

#: Job boards that aggregate public-sector postings rather than employ anyone.
EXCLUDED_SOURCES = frozenset(
    {"usagov", "governmentjobs", "winocular", "schoolspring", "peopleadmin", "oraclepeoplesoft"}
)

EXCLUDED_TLDS = (".gov", ".mil", ".edu")

#: Clearance above Public Trust, wherever it appears. The structured
#: ``security_clearance`` field reads "None" on roles that demand TS/SCI - about
#: one in twelve - so this text check is the guarantee, not a backstop.
CLEARANCE = re.compile(
    r"top secret|ts/sci|ts-sci|\bsci\b|\bsecret\b|polygraph|\bpoly\b|doe level q"
    r"|dod clearance|active clearance|\bcleared\b|clearance required",
    re.I,
)

#: Tools belonging to some other engineering discipline. Used only as an
#: exclusion and only when no software tool appears alongside: as a positive
#: signal a tool vocabulary is brittle, but as a negative one it is precise.
NON_SOFTWARE_TOOLS = re.compile(
    r"solidworks|autocad|revit|\bcad\b|creo|comsol|ansys|staad|bluebeam|\bbim\b|\bfea\b"
    r"|finite element|microscopy|elisa|hplc|mass spectrom|flow cytometry|qpcr|cell culture"
    r"|chromatograph|\bcfd\b|solumina|tipqa|resrad|microshield|lumerical|klayout"
    r"|\bvhdl\b|verilog|xilinx|\bzynq\b|quartus|vivado|altera",
    re.I,
)

#: Backend, platform, and data-infrastructure signal. Java and Spring count
#: exactly as much as Go and Kubernetes.
BACKEND_TOOLS = re.compile(
    r"python|java(?!script)|kotlin|golang|\bgo\b|rust|c\+\+|kubernetes|docker|terraform"
    r"|kafka|postgres|mysql|spring|django|fastapi|aws|azure|gcp|linux|ci/cd|jenkins|redis"
    r"|graphql|grpc|microservice|airflow|spark|helm|ansible|istio|envoy|lambda|dynamodb"
    r"|typescript|node|scala|elasticsearch|rabbitmq|snowflake|databricks",
    re.I,
)

#: Build tooling. Its presence rescues a posting from the governance and
#: IT-administration checks below: plenty of real platform work touches FedRAMP
#: or Active Directory, and what marks a compliance role is that the framework
#: names are all there is.
BUILD_TOOLS = re.compile(
    r"kubernetes|terraform|docker|kafka|spring|django|fastapi|golang|\brust\b|\bgrpc\b"
    r"|airflow|spark|helm|istio|envoy|lambda|dynamodb|postgres|redis|microservice"
    r"|ci/cd|jenkins|gitlab ci|github actions",
    re.I,
)

#: Governance and compliance frameworks.
GRC_TOOLS = re.compile(
    r"fedramp|\brmf\b|fisma|nist 800|nist sp|nist csf|cis controls|\bstig\b|poa&m"
    r"|\bato\b|soc 2|iso 27001|mitre att&ck|\bsiem\b|onetrust|\bgrc\b|\bfips\b",
    re.I,
)

#: Enterprise IT administration platforms, as distinct from build tooling.
IT_ADMIN_TOOLS = re.compile(
    r"microsoft 365|\bm365\b|entra id|\bintune\b|active directory|configuration manager"
    r"|\bsccm\b|vmware|citrix|servicenow|sharepoint|microsoft exchange|\bmdm\b"
    r"|microsoft office|group policy",
    re.I,
)

FRONTEND_TITLE = re.compile(r"full.?stack|front.?end|ui engineer|web develop", re.I)

#: Customer-facing engineering. These postings list Python and AWS because
#: they demo and integrate them, not because the job is building systems. The
#: tools cannot tell the difference; the title can.
CUSTOMER_FACING_TITLE = re.compile(
    r"solutions? engineer|sales engineer|pre.?sales|account manager|customer success"
    r"|technical success|success engineer|transformation success|field engineer"
    r"|field application|implementation consultant|technical account",
    re.I,
)

#: Analyst and research titles, suppressed when an engineering token is also
#: present: "AI Engineer/Scientist" is an engineering role that happens to
#: contain a research word, and the tokens can appear in either order.
ANALYST_TITLE = re.compile(
    r"\banalyst\b|\bresearcher\b|business intelligence|ux research|data scientist"
    r"|epidemiolog|psychologist|economist|\bscientist\b|modeler|\banalytics\b"
    r"|\bspecialist\b|\bofficer\b",
    re.I,
)

ENGINEERING_TITLE = re.compile(r"\bengineer|\bdeveloper\b|\barchitect\b|\bsre\b", re.I)

#: Hardware and physical engineering. Distinct from the tools check, since
#: these postings often carry Python or MATLAB and would otherwise pass.
HARDWARE_TITLE = re.compile(
    r"\brf\b|\bdsp\b|\bfpga\b|\basic\b|photonic|elint|\bsigint\b|antenna|radar"
    r"|nuclear|mechanical|structural|electrical|firmware|embedded|manufactur|test engineer"
    r"|hardware|circuit|\bpcb\b|thermal|propulsion|spacecraft|flight",
    re.I,
)

#: Network and IT operations. Real engineering, but not the backend and
#: platform track being targeted.
OPS_TITLE = re.compile(
    r"network (reliability |)engineer|\bdns\b|system administrator|sysadmin|help ?desk"
    r"|desktop|endpoint|workstation|service management|\bitsm\b|technician|noc\b",
    re.I,
)

#: Management corroboration for the People Manager flag.
MANAGEMENT_TITLE = re.compile(
    r"\bmanager\b|\bdirector\b|head of|\bvp\b|vice president|\bchief\b|supervisor"
    r"|\bsupervisory\b|team lead\b|\bforeman\b",
    re.I,
)
MANAGEMENT_LANGUAGE = re.compile(
    r"direct reports?|supervis|performance review|managing (?:people|teams|staff)"
    r"|manage (?:a )?(?:team|staff|people)|leading a team|hiring and|people manage"
    r"|team of \d|budget responsib|\bp&l\b",
    re.I,
)

#: Title level markers that indicate a senior individual contributor.
SENIOR_LEVELS = frozenset(
    {"sr", "staff", "principal", "lead", "distinguished", "fellow", "iii", "iv", "v", "3", "4", "5"}
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    """A posting plus why it landed where it did."""

    posting: Posting
    band: str
    reject_reasons: tuple[str, ...] = ()
    demote_reasons: tuple[str, ...] = ()

    def as_record(self) -> JsonDict:
        """Flatten for JSONL: verdict fields alongside the posting's own.

        Flat rather than nested so the output stays greppable - "which
        postings were rejected only on compensation" should be answerable with
        a one-liner, not a parser.
        """
        return {
            **self.posting.as_record(),
            "band": self.band,
            "reject_reasons": list(self.reject_reasons),
            "demote_reasons": list(self.demote_reasons),
        }


def _tools(posting: Posting) -> str:
    return " | ".join(posting.tools)


def _clearance_text(posting: Posting) -> str:
    """Everywhere a clearance requirement can hide.

    ``raw_title`` is included deliberately: the core title is normalized, and a
    posting titled "Mission Engineer, Cleared" loses the only word that says so.
    """
    return " ".join(
        (
            " | ".join(posting.certifications),
            posting.requirements,
            posting.title,
            posting.raw_title,
        )
    )


def manages_people(posting: Posting) -> bool:
    """Whether the People Manager flag is corroborated by title or requirements.

    The flag alone reads IC ladder titles - "Principal Engineer", "Sr. Lead
    Software Engineer" - as management, on roughly a quarter of the postings
    it marks. Requiring a second signal keeps the reject for real management
    roles while returning the misread ones to ``possible``.
    """
    if posting.role_type != "People Manager":
        return False
    titles = f"{posting.title} {posting.raw_title}"
    return bool(MANAGEMENT_TITLE.search(titles) or MANAGEMENT_LANGUAGE.search(posting.requirements))


def title_says_senior(posting: Posting) -> bool:
    """Whether the posting's own title carries a senior-IC level marker.

    Read alongside ``seniority_level`` rather than instead of it. The vendor
    field calls a "Senior Electronics Hardware Engineer" entry level and a
    "Systems and Infrastructure Engineer III" entry level, so neither signal
    can be trusted alone.
    """
    return bool(set(level_signature(posting.raw_title).split("+")) & SENIOR_LEVELS)


def reject_reasons(posting: Posting, settings: ScreenSettings) -> list[str]:
    """Every hard-reject reason that applies, not just the first."""
    reasons: list[str] = []

    if posting.is_expired:
        reasons.append("expired")
    if posting.source.lower() in EXCLUDED_SOURCES:
        reasons.append("public-sector source")
    if posting.company_host and posting.company_host.endswith(EXCLUDED_TLDS):
        reasons.append("gov/mil/edu domain")
    if "Full Time" not in posting.commitment:
        reasons.append("not full-time")
    if posting.security_clearance not in (None, "None", "Public Trust", "Other"):
        reasons.append("clearance field")
    if CLEARANCE.search(_clearance_text(posting)):
        reasons.append("clearance in text")
    if posting.employer_type == "External Position":
        reasons.append("staffing placement")
    if manages_people(posting):
        reasons.append("people manager")

    tools = _tools(posting)
    if NON_SOFTWARE_TOOLS.search(tools) and not BACKEND_TOOLS.search(tools):
        reasons.append("non-software discipline")
    if FRONTEND_TITLE.search(posting.title):
        reasons.append("full-stack/frontend title")
    if posting.company.lower() in settings.company_blocklist:
        reasons.append("blocked company")
    if posting.comp_max is not None and posting.comp_max < settings.comp_floor:
        reasons.append("comp below floor")
    return reasons


def demote_reasons(posting: Posting, settings: ScreenSettings) -> list[str]:
    """Signals that keep a posting out of ``strong`` without discarding it."""
    reasons: list[str] = []

    if posting.role_type == "People Manager" and not manages_people(posting):
        reasons.append("uncorroborated people-manager flag")

    title = posting.title
    if CUSTOMER_FACING_TITLE.search(title):
        reasons.append("customer-facing title")
    if ANALYST_TITLE.search(title) and not ENGINEERING_TITLE.search(title):
        reasons.append("analyst/research title")
    if HARDWARE_TITLE.search(title):
        reasons.append("hardware/physical title")
    if OPS_TITLE.search(title):
        reasons.append("network/IT ops title")

    tools = _tools(posting)
    if not BUILD_TOOLS.search(tools):
        if GRC_TOOLS.search(tools):
            reasons.append("compliance/GRC tooling")
        if IT_ADMIN_TOOLS.search(tools):
            reasons.append("IT-admin tooling")

    # A wide band or a long city list means the stated ceiling belongs to the
    # priciest metro rather than to this one. Measured: postings spanning five
    # or more cities have a median band spread of 53% against 30% for a single
    # city. The maximum still cleared the floor, so this demotes rather than
    # rejects.
    if posting.comp_min is not None and posting.comp_min < settings.comp_min_floor:
        reasons.append("band bottom below floor")
    if posting.city_count >= settings.wide_geography_cities:
        reasons.append("geo-tiered pay band")
    return reasons


def meets_strong_criteria(posting: Posting, settings: ScreenSettings) -> bool:
    """Whether a posting satisfies the strong bar, ignoring hard rejects.

    Separated from banding so that "would be strong except for X" is
    answerable: re-running the full band would just hit the same reject again.
    """
    if not BACKEND_TOOLS.search(_tools(posting)):
        return False
    if demote_reasons(posting, settings):
        return False
    return (
        posting.seniority_level == "Senior Level"
        or title_says_senior(posting)
        or (posting.comp_max is not None and posting.comp_max >= settings.comp_floor)
    )


def assign_band(posting: Posting, settings: ScreenSettings) -> Verdict:
    """Decide a posting's band and record every reason behind it."""
    rejects = reject_reasons(posting, settings)
    if rejects:
        return Verdict(posting, BAND_REJECTED, tuple(rejects))

    demotes = demote_reasons(posting, settings)
    strong = meets_strong_criteria(posting, settings)
    return Verdict(posting, BAND_STRONG if strong else BAND_POSSIBLE, (), tuple(demotes))


def would_be_strong_but_for(verdict: Verdict, reason: str, settings: ScreenSettings) -> bool:
    """Whether a posting is rejected on one reason alone and otherwise strong.

    Exists so the compensation floor can be checked against real postings
    after it moves, rather than trusted because the number sounds about right.
    """
    if verdict.reject_reasons != (reason,):
        return False
    return meets_strong_criteria(verdict.posting, settings)


@dataclass(frozen=True)
class ScreenResult:
    screened_path: Path
    meta_path: Path
    counts: dict[str, int] = field(default_factory=dict)
    reject_reasons: dict[str, int] = field(default_factory=dict)
    demote_reasons: dict[str, int] = field(default_factory=dict)
    near_misses: int = 0


def screen(postings: Sequence[Posting], settings: ScreenSettings) -> list[Verdict]:
    """Band every posting. Nothing is dropped."""
    return [assign_band(posting, settings) for posting in postings]


def run_screen(
    records: Iterable[Any],
    out_dir: Path,
    settings: ScreenSettings,
    *,
    source_path: Path | None = None,
    compress: bool = True,
) -> ScreenResult:
    """Screen normalized postings, writing every band plus a meta sidecar."""
    from collections import Counter

    postings = [Posting.from_record(record) for record in records]
    verdicts = screen(postings, settings)

    band_counts: Counter[str] = Counter(v.band for v in verdicts)
    reject_counts: Counter[str] = Counter()
    demote_counts: Counter[str] = Counter()
    for verdict in verdicts:
        reject_counts.update(verdict.reject_reasons)
        demote_counts.update(verdict.demote_reasons)
    near = sum(1 for v in verdicts if would_be_strong_but_for(v, "comp below floor", settings))

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    screened_path = with_compression(out_dir / f"screened-{stamp}.jsonl", compress)
    meta_path = out_dir / f"screen-meta-{stamp}.json"

    order = {BAND_STRONG: 0, BAND_POSSIBLE: 1, BAND_REJECTED: 2}
    verdicts.sort(key=lambda v: (order[v.band], -(v.posting.comp_max or 0), v.posting.company))

    with JsonlWriter(screened_path) as writer:
        for verdict in verdicts:
            writer.write(verdict.as_record())

    meta: JsonDict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(source_path) if source_path else None,
        "screened_file": screened_path.name,
        "compressed": compress,
        "settings": {
            "comp_floor": settings.comp_floor,
            "comp_min_floor": settings.comp_min_floor,
            "wide_geography_cities": settings.wide_geography_cities,
            "company_blocklist": sorted(settings.company_blocklist),
        },
        "bands": dict(band_counts),
        "reject_reasons": dict(reject_counts.most_common()),
        "demote_reasons": dict(demote_counts.most_common()),
        "strong_except_for_comp": near,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "screened %d postings: %d strong, %d possible, %d rejected",
        len(postings),
        band_counts[BAND_STRONG],
        band_counts[BAND_POSSIBLE],
        band_counts[BAND_REJECTED],
    )
    return ScreenResult(
        screened_path=screened_path,
        meta_path=meta_path,
        counts=dict(band_counts),
        reject_reasons=dict(reject_counts.most_common()),
        demote_reasons=dict(demote_counts.most_common()),
        near_misses=near,
    )
