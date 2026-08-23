#!/usr/bin/env python3
"""Dry-run the screening rules over a raw scrape and report what they do.

This is a measuring instrument, not the pipeline. Every threshold and word
list below is meant to be edited and the script re-run: the point is to see
what a rule change does to real records before it is written into the screen
stage, where a bad rule silently deletes jobs you would have wanted.

Reject reasons are collected in full rather than stopping at the first match,
so "would have been strong except for compensation" is answerable. That set is
the one to read after any threshold change.

Usage:
    python3 scripts/screen_funnel.py data/job_shortlist/raw/jobs-*.jsonl.gz
    python3 scripts/screen_funnel.py <jobs file> --comp-floor 180000 --list strong
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

# --------------------------------------------------------------------------
# Tunable rules. Edit these, re-run, compare.
# --------------------------------------------------------------------------

#: Below this, a posting with a stated ceiling is rejected. Postings that state
#: nothing are never rejected on compensation - they are a large and good slice
#: of results, not noise.
DEFAULT_COMP_FLOOR = 180_000

#: Yearly figures outside this band are treated as missing. The data contains
#: an $81,337,000 "yearly" salary and hourly rates mislabeled as yearly.
COMP_PLAUSIBLE = (25_000, 600_000)

#: ATS sources that are public-sector job boards rather than employers.
EXCLUDED_SOURCES = frozenset(
    {"usagov", "governmentjobs", "winocular", "schoolspring", "peopleadmin", "oraclepeoplesoft"}
)

EXCLUDED_TLDS = (".gov", ".mil", ".edu")

#: Clearance above Public Trust, wherever it is mentioned. The structured
#: security_clearance field says "None" on roles that demand TS/SCI, so this
#: text re-check is the actual guarantee rather than a backstop.
CLEARANCE = re.compile(
    r"top secret|ts/sci|ts-sci|\bsci\b|\bsecret\b|polygraph|\bpoly\b|doe level q"
    r"|dod clearance|active clearance",
    re.I,
)

#: Tools that mark a posting as some other engineering discipline. Used only
#: as an exclusion, and only when no software tool appears alongside: a
#: negative signal is far more reliable here than a positive one.
NON_SOFTWARE = re.compile(
    r"solidworks|autocad|revit|\bcad\b|creo|comsol|ansys|staad|bluebeam|\bbim\b|\bfea\b"
    r"|finite element|microscopy|elisa|hplc|mass spectrom|flow cytometry|qpcr|cell culture"
    r"|chromatograph|\bcfd\b|solumina|tipqa|resrad|microshield|lumerical|klayout"
    r"|\bvhdl\b|verilog|xilinx|\bzynq\b|quartus|vivado|altera|cadence virtuoso",
    re.I,
)

#: Backend, platform, infrastructure, and data-infrastructure signal. Java and
#: Spring count exactly as much as Go and Kubernetes.
BACKEND = re.compile(
    r"python|java(?!script)|kotlin|golang|\bgo\b|rust|c\+\+|kubernetes|docker|terraform"
    r"|kafka|postgres|mysql|spring|django|fastapi|aws|azure|gcp|linux|ci/cd|jenkins|redis"
    r"|graphql|grpc|microservice|airflow|spark|helm|ansible|istio|envoy|lambda|dynamodb"
    r"|typescript|node|scala|elasticsearch|rabbitmq|snowflake|databricks",
    re.I,
)

FRONTEND_TITLE = re.compile(r"full.?stack|front.?end|ui engineer|web develop", re.I)

#: Customer-facing engineering. These roles list Python and AWS because they
#: demo and integrate them, not because the job is building systems. The tools
#: field cannot tell the difference; the title can.
CUSTOMER_FACING_TITLE = re.compile(
    r"solutions? engineer|sales engineer|pre.?sales|account manager|customer success"
    r"|technical success|success engineer|transformation success|field engineer"
    r"|field application|implementation consultant"
    r"|solutions? architect(?!.*\b(platform|cloud|data)\b)|technical account",
    re.I,
)

#: Analyst and research titles. Suppressed when the title also carries an
#: engineering token: "AI Engineer/Scientist" is an engineering role that
#: happens to contain a research word, and a naive lookahead misses it because
#: the tokens can appear in either order.
ANALYST_TITLE = re.compile(
    r"\banalyst\b|\bresearcher\b|business intelligence|ux research|data scientist"
    r"|epidemiolog|psychologist|economist|\bscientist\b|modeler|\banalytics\b"
    r"|\bspecialist\b|\bofficer\b",
    re.I,
)

#: Hardware, RF, and physical engineering. Distinct from NON_SOFTWARE, which
#: reads the tools field: these titles often carry Python or MATLAB and would
#: otherwise pass the backend check.
HARDWARE_TITLE = re.compile(
    r"\brf\b|\bdsp\b|\bfpga\b|\basic\b|photonic|elint|\bsigint\b|antenna|radar"
    r"|nuclear|mechanical|structural|electrical|firmware|embedded|manufactur|test engineer"
    r"|hardware|circuit|\bpcb\b|thermal|propulsion|spacecraft|flight",
    re.I,
)

#: Network and IT operations. Running BGP or DNS at scale is real engineering,
#: but it is not the backend/platform track being targeted here.
#: Compliance and governance frameworks. A posting whose tool list is these
#: describes assessing systems, not building them, however much cloud
#: vocabulary sits alongside.
GRC_TOOLS = re.compile(
    r"fedramp|\brmf\b|fisma|nist 800|nist sp|nist csf|cis controls|\bstig\b|poa&m"
    r"|\bato\b|soc 2|iso 27001|mitre att&ck|\bsiem\b|onetrust|\bgrc\b|\bfips\b",
    re.I,
)

#: Enterprise IT administration platforms, as distinct from build tooling.
IT_PLATFORM_TOOLS = re.compile(
    r"microsoft 365|\bm365\b|entra id|\bintune\b|active directory|configuration manager"
    r"|\bsccm\b|vmware|citrix|servicenow|sharepoint|microsoft exchange|\bmdm\b"
    r"|microsoft office|group policy",
    re.I,
)

#: Build tooling. Its presence rescues a posting from the GRC and IT-platform
#: checks: a security engineer who writes Terraform is building something.
BUILD_TOOLS = re.compile(
    r"kubernetes|terraform|docker|kafka|spring|django|fastapi|golang|\brust\b|\bgrpc\b"
    r"|airflow|spark|helm|istio|envoy|lambda|dynamodb|postgres|redis|microservice"
    r"|ci/cd|jenkins|gitlab ci|github actions",
    re.I,
)

#: Engineering tokens that exempt a title from the analyst rule.
ENGINEERING_TITLE = re.compile(r"\bengineer|\bdeveloper\b|\barchitect\b|\bsre\b", re.I)

OPS_TITLE = re.compile(
    r"network (reliability |)engineer|\bdns\b|system administrator|sysadmin|help ?desk"
    r"|desktop|endpoint|workstation|service management|\bitsm\b|technician|noc\b",
    re.I,
)

#: Seniority and level markers, read from the raw posting title. Kept in sync
#: with the normalize stage: core_job_title has these stripped, so the raw
#: title is the only place a level survives.
LEVEL_WORD = re.compile(
    r"\b(?:junior|jr|associate|entry|mid|senior|snr|sr|staff|principal|lead|distinguished"
    r"|fellow|apprentice|intern|trainee)\b",
    re.I,
)
LEVEL_NUMBER = re.compile(
    r"(?:^|[\s\-(,/])(?:l|t)?(i{1,3}|iv|vi{0,3}|[1-5](?:[._]\d)?)(?=$|[\s\-),/])",
    re.I,
)

#: Title levels that indicate a senior individual contributor.
SENIOR_LEVELS = frozenset(
    {"sr", "staff", "principal", "lead", "distinguished", "fellow", "iii", "iv", "v", "3", "4", "5"}
)

#: Management corroboration for role_type. The field alone reads "Lead" and
#: "Principal" as management, which are IC ladder titles at many employers.
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

#: Companies never worth surfacing. Unlike company discovery, where a bad
#: company costs one glance, a staffing firm here can flood the list every day.
COMPANY_BLOCKLIST: frozenset[str] = frozenset()

# --------------------------------------------------------------------------


def read_records(path: Path) -> Iterator[JsonDict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: invalid JSON: {exc}") from exc


def sub(record: JsonDict, key: str) -> JsonDict:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def joined(value: Any) -> str:
    return " | ".join(text(v) for v in value) if isinstance(value, list) else ""


def comp_max(record: JsonDict) -> int | None:
    """Stated yearly ceiling, or None when absent or implausible."""
    value = sub(record, "v5_processed_job_data").get("yearly_max_compensation")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return int(value) if COMP_PLAUSIBLE[0] < value < COMP_PLAUSIBLE[1] else None


def raw_title(record: JsonDict) -> str:
    """The posting's own title, which retains the level core_job_title drops."""
    info = sub(record, "job_information")
    return text(info.get("job_title_raw")) or text(info.get("title"))


def level_signature(title: str) -> str:
    """Level tokens from a raw title, canonicalized and sorted."""
    words = {m.group(0).lower().rstrip(".") for m in LEVEL_WORD.finditer(title)}
    words = {"sr" if w in {"senior", "snr"} else "jr" if w == "junior" else w for w in words}
    numbers = {m.group(1).lower().replace("_", ".") for m in LEVEL_NUMBER.finditer(title)}
    return "+".join(sorted(words | numbers))


def title_says_senior(record: JsonDict) -> bool:
    """Whether the raw title carries a senior-IC level marker.

    Used alongside seniority_level rather than instead of it: the vendor field
    calls a "Senior Electronics Hardware Engineer" entry level and a "Desktop
    Support Technician II" senior, in opposite directions, so neither signal
    is trustworthy alone.
    """
    return bool(set(level_signature(raw_title(record)).split("+")) & SENIOR_LEVELS)


def manages_people(record: JsonDict) -> bool:
    """Whether People Manager is corroborated by the title or the requirements.

    role_type on its own misreads IC ladder titles - "Principal Engineer",
    "Sr. Lead Software Engineer" - as management. Requiring a second signal
    keeps the reject while recovering the misclassified.
    """
    job = sub(record, "v5_processed_job_data")
    if text(job.get("role_type")) != "People Manager":
        return False
    titles = f"{text(job.get('core_job_title'))} {raw_title(record)}"
    return bool(
        MANAGEMENT_TITLE.search(titles)
        or MANAGEMENT_LANGUAGE.search(text(job.get("requirements_summary")))
    )


def company(record: JsonDict) -> str:
    return (
        text(sub(record, "enriched_company_data").get("name"))
        or text(sub(record, "v5_processed_job_data").get("company_name"))
        or "(unknown)"
    )


def reject_reasons(record: JsonDict, comp_floor: int) -> list[str]:
    """Every applicable reason, not just the first.

    Collecting all of them is what makes "strong except for compensation"
    a queryable set rather than a number hidden behind whichever check ran
    first.
    """
    job = sub(record, "v5_processed_job_data")
    tools = joined(job.get("technical_tools"))
    haystack = " ".join(
        (
            joined(job.get("licenses_or_certifications")),
            text(job.get("requirements_summary")),
            text(job.get("core_job_title")),
        )
    )
    reasons: list[str] = []

    if record.get("is_expired") is True:
        reasons.append("expired")
    if text(record.get("source")).lower() in EXCLUDED_SOURCES:
        reasons.append("public-sector source")
    if (
        text(sub(record, "enriched_company_data").get("homepage_uri"))
        .rstrip("/")
        .endswith(EXCLUDED_TLDS)
    ):
        reasons.append("gov/mil/edu domain")
    if "Full Time" not in (job.get("commitment") or []):
        reasons.append("not full-time")
    if text(job.get("security_clearance")) not in ("None", "Public Trust", "Other"):
        reasons.append("clearance field")
    if CLEARANCE.search(haystack):
        reasons.append("clearance in text")
    if manages_people(record):
        reasons.append("people manager")
    if text(job.get("position_employer_type")) == "External Position":
        reasons.append("staffing placement")
    if NON_SOFTWARE.search(tools) and not BACKEND.search(tools):
        reasons.append("non-software discipline")
    if FRONTEND_TITLE.search(text(job.get("core_job_title"))):
        reasons.append("full-stack/frontend title")
    if company(record).lower() in COMPANY_BLOCKLIST:
        reasons.append("blocked company")
    value = comp_max(record)
    if value is not None and value < comp_floor:
        reasons.append("comp below floor")
    return reasons


def demote_reasons(record: JsonDict) -> list[str]:
    """Soft signals that keep a posting out of `strong` without rejecting it.

    Role shape is read from the title, which is a written-by-humans field, not
    a fact. "Solution Architect" is pre-sales at a consultancy and real
    architecture at a product company; the same words mean different jobs.
    Rejecting on that would repeat the mistake these rules exist to catch -
    deleting records on an unreliable signal - so a match demotes to
    `possible`, where the posting is still read.
    """
    role_title = text(sub(record, "v5_processed_job_data").get("core_job_title"))
    reasons: list[str] = []
    if text(sub(record, "v5_processed_job_data").get("role_type")) == "People Manager":
        # Uncorroborated: not rejected, but not strong either.
        reasons.append("uncorroborated people-manager flag")
    if CUSTOMER_FACING_TITLE.search(role_title):
        reasons.append("customer-facing title")
    if ANALYST_TITLE.search(role_title) and not ENGINEERING_TITLE.search(role_title):
        reasons.append("analyst/research title")
    if HARDWARE_TITLE.search(role_title):
        reasons.append("hardware/physical title")
    if OPS_TITLE.search(role_title):
        reasons.append("network/IT ops title")

    tools = joined(sub(record, "v5_processed_job_data").get("technical_tools"))
    if not BUILD_TOOLS.search(tools):
        # Only meaningful in the absence of build tooling. Plenty of real
        # platform work touches FedRAMP or Active Directory; what marks a
        # compliance role is that the framework names are all there is.
        if GRC_TOOLS.search(tools):
            reasons.append("compliance/GRC tooling")
        if IT_PLATFORM_TOOLS.search(tools):
            reasons.append("IT-admin tooling")
    return reasons


def is_strong(record: JsonDict, comp_floor: int) -> bool:
    job = sub(record, "v5_processed_job_data")
    if not BACKEND.search(joined(job.get("technical_tools"))):
        return False
    if demote_reasons(record):
        return False
    value = comp_max(record)
    return (
        text(job.get("seniority_level")) == "Senior Level"
        or title_says_senior(record)
        or (value is not None and value >= comp_floor)
    )


def dedup_key(record: JsonDict) -> Any:
    job = sub(record, "v5_processed_job_data")
    return record.get("liberal_dedup_cluster") or (
        company(record),
        " ".join(text(job.get("core_job_title")).lower().split()),
        tuple(sorted(text(c) for c in (job.get("workplace_cities") or []))),
    )


def section(name: str) -> None:
    print(f"\n{name}\n{'-' * len(name)}")


def show(record: JsonDict) -> str:
    job = sub(record, "v5_processed_job_data")
    value = comp_max(record)
    money = f"${value:>7,}" if value else "    n/a"
    tools = ", ".join(text(t) for t in (job.get("technical_tools") or [])[:5])
    return (
        f"  {money}  {text(job.get('core_job_title'))[:44]:44s} "
        f"{company(record)[:26]:26s} {tools[:46]}"
    )


def run_audit(records: list[JsonDict], which: str, limit: int) -> None:
    """Measure how often a vendor-inferred field disagrees with the posting."""
    if which == "role-type":
        flagged = [
            r
            for r in records
            if text(sub(r, "v5_processed_job_data").get("role_type")) == "People Manager"
        ]
        suspect = [r for r in flagged if not manages_people(r)]
        engineering = [
            r
            for r in suspect
            if BACKEND.search(joined(sub(r, "v5_processed_job_data").get("technical_tools")))
        ]
        section("role_type reliability")
        print(f"  role_type == People Manager        {len(flagged):6d}")
        print(
            f"  uncorroborated by title or text    {len(suspect):6d}"
            f"   {len(suspect) / max(len(flagged), 1) * 100:5.1f}% of flagged"
        )
        print(
            f"  ...and backend-tooled              {len(engineering):6d}"
            f"   {len(engineering) / max(len(flagged), 1) * 100:5.1f}% of flagged,"
            f" {len(engineering) / len(records) * 100:.1f}% of scrape"
        )
        print("\n  These are hard-rejected today and would be recovered by requiring")
        print("  corroboration. Read a few before deciding they are worth recovering.\n")
        for record in sorted(engineering, key=lambda r: -(comp_max(r) or 0))[:limit]:
            print(show(record))
            print(f"            raw title: {raw_title(record)[:88]}")
        return

    section("seniority_level vs the raw title's level marker")
    rows = [
        (
            r,
            text(sub(r, "v5_processed_job_data").get("seniority_level")),
            level_signature(raw_title(r)),
        )
        for r in records
    ]
    titled = [(r, lvl, sig) for r, lvl, sig in rows if sig]
    senior_title = [(r, lvl, sig) for r, lvl, sig in titled if title_says_senior(r)]
    missed = [(r, lvl, sig) for r, lvl, sig in senior_title if lvl != "Senior Level"]
    junior_title = [
        (r, lvl, sig)
        for r, lvl, sig in titled
        if not title_says_senior(r) and lvl == "Senior Level"
    ]
    print(f"  records with a level marker in the title   {len(titled):6d}")
    print(
        f"  title says senior, field does not          {len(missed):6d}"
        f"   (would be wrongly demoted)"
    )
    print(
        f"  field says senior, title says otherwise    {len(junior_title):6d}"
        f"   (would be wrongly promoted)"
    )
    print("\n  Both directions matter: the first loses roles, the second floods the list.\n")
    for label, group in (("MISSED", missed), ("PROMOTED", junior_title)):
        print(f"  --- {label} ---")
        for record, lvl, sig in group[: limit // 2]:
            print(
                f"  field={lvl or 'None':12s} title-level={sig:12s} "
                f"{raw_title(record)[:56]:56s} {company(record)[:20]}"
            )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs", type=Path)
    parser.add_argument("--comp-floor", type=int, default=DEFAULT_COMP_FLOOR)
    parser.add_argument(
        "--list",
        choices=["strong", "possible", "near-miss", "none"],
        default="strong",
        help="Which surviving set to print in full. 'near-miss' is rejected on "
        "compensation alone and otherwise strong.",
    )
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument(
        "--audit",
        choices=["role-type", "seniority"],
        help="Print a field-reliability audit instead of the funnel. 'role-type' "
        "lists People Manager records that look like IC engineering roles; "
        "'seniority' compares seniority_level against the raw title's level marker.",
    )
    args = parser.parse_args()

    records = list(read_records(args.jobs))
    if not records:
        raise SystemExit(f"{args.jobs}: no records")
    floor = args.comp_floor

    if args.audit:
        run_audit(records, args.audit, args.limit)
        return

    reasons_by_record = [(r, reject_reasons(r, floor)) for r in records]
    survivors_raw = [r for r, reasons in reasons_by_record if not reasons]

    seen: set[Any] = set()
    survivors: list[JsonDict] = []
    duplicates = 0
    for record in survivors_raw:
        key = dedup_key(record)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        survivors.append(record)

    strong = [r for r in survivors if is_strong(r, floor)]
    possible = [r for r in survivors if not is_strong(r, floor)]

    print(f"file: {args.jobs}    records: {len(records)}    comp floor: ${floor:,}")

    section("Reject reasons (a record may have several)")
    counts: Counter[str] = Counter()
    for _, reasons in reasons_by_record:
        counts.update(reasons)
    for reason, count in counts.most_common():
        print(f"  {reason:28s} {count:6d}   {count / len(records) * 100:5.1f}%")
    only: Counter[str] = Counter()
    for _, reasons in reasons_by_record:
        if len(reasons) == 1:
            only[reasons[0]] += 1
    print("\n  Sole reason for rejection (what each rule uniquely removes):")
    for reason, count in only.most_common():
        print(f"  {reason:28s} {count:6d}")

    section("Demotions (strong -> possible; never rejected)")
    demotions: Counter[str] = Counter()
    for record in survivors:
        demotions.update(demote_reasons(record))
    for reason, count in demotions.most_common():
        print(f"  {reason:28s} {count:6d}")
    if not demotions:
        print("  none")

    section("Funnel")
    rejected = len(records) - len(survivors_raw)
    print(f"  scraped                      {len(records):6d}")
    print(f"  - rejected                   {-rejected:6d}   -> {len(survivors_raw):6d}")
    print(f"  - duplicates                 {-duplicates:6d}   -> {len(survivors):6d}")
    print(
        f"\n  strong    {len(strong):5d}"
        f"   (comp >= floor: {sum(1 for r in strong if comp_max(r))},"
        f" comp unknown: {sum(1 for r in strong if comp_max(r) is None)})"
    )
    print(
        f"  possible  {len(possible):5d}"
        f"   (comp unknown: {sum(1 for r in possible if comp_max(r) is None)})"
    )
    if survivors:
        print(
            f"\n  {len(strong) / len(records) * 100:.1f}% of the scrape reaches 'strong'"
            f"; {len(records) / max(len(strong), 1):.0f}x reduction"
        )

    section("Near misses: rejected on compensation alone, otherwise strong")
    near = [
        r for r, reasons in reasons_by_record if reasons == ["comp below floor"] and is_strong(r, 0)
    ]
    seen_near: set[Any] = set()
    deduped_near: list[JsonDict] = []
    for record in near:
        key = dedup_key(record)
        if key not in seen_near:
            seen_near.add(key)
            deduped_near.append(record)
    near = deduped_near
    print(f"  {len(near)} postings. Raising or lowering the floor moves these.")
    for record in sorted(near, key=lambda r: -(comp_max(r) or 0))[:12]:
        print(show(record))

    section("Where survivors come from")
    cats = Counter(text(sub(r, "v5_processed_job_data").get("job_category")) for r in survivors)
    for name, count in cats.most_common():
        print(f"  {name:38s} {count:5d}")
    firms = Counter(company(r) for r in survivors)
    repeat = [(n, c) for n, c in firms.most_common(12) if c > 1]
    if repeat:
        print("\n  Companies with the most surviving postings (blocklist candidates):")
        for name, count in repeat:
            print(f"  {name:38s} {count:5d}")

    if args.list != "none":
        chosen = {"strong": strong, "possible": possible, "near-miss": near}[args.list]
        section(f"{args.list} ({len(chosen)}, showing up to {args.limit})")
        ordered = sorted(chosen, key=lambda r: -(comp_max(r) or 0))
        for record in ordered[: args.limit]:
            print(show(record))

    section("Reminder")
    print("  Every rule here is a guess until it is read against real postings.")
    print("  Check the near-miss list after any threshold change, and skim 'possible'")
    print("  for roles that should have been strong - that is the failure that")
    print("  costs you something, and it is invisible unless you look for it.")


if __name__ == "__main__":
    main()
