"""Deriving a company's careers page from a posting's apply URL.

Records point at individual job postings, but the useful thing to visit is the
employer's board, which lists everything they have open. There is no field for
it, so it is derived from ``apply_url`` and ``source``.

ATS URL shapes fall into three tiers:

``HOST``
    The host itself identifies the employer (``niyamit.bamboohr.com``,
    ``careers-iridium.icims.com``, ``jobs.dish.com``). Trimming to the host
    root lands on their board. This is also the default for unrecognized
    sources, which is why a long tail of one-off ATSes is manageable.

``HOST_AND_SEGMENTS``
    The employer is a path segment (``job-boards.greenhouse.io/ionq``,
    ``jobs.lever.co/agile-defense``). Trimming to the host would land on the
    ATS vendor's own site, so a fixed number of path segments is kept.

``BOARD``
    The employer is identified by a query parameter, but the vendor's board
    URL can be rebuilt from it by keeping the employer parameter, dropping the
    job-specific ones, and (for some vendors) swapping the detail path for the
    listing path. ADP's ``cid`` and Paycom's ``clientkey`` work this way.

``POSTING``
    Same situation, but with no reliable way to rebuild the listing URL, so the
    posting URL is used as-is: one extra click to reach the employer's board,
    versus a link that goes nowhere useful.

Every result is a heuristic that can 404, which is why the rendered output
keeps the company website as a fallback link.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from hiringcafe_toolkit.common.urls import host_to_url, normalize_host


class DerivationTier(StrEnum):
    """How a careers link was derived, recorded for run diagnostics."""

    HOST = "host"
    HOST_AND_SEGMENTS = "host_and_segments"
    BOARD = "board"
    POSTING = "posting"
    NONE = "none"


#: Preference order when the same company is reachable through more than one
#: applicant tracking system. A company posting through two vendors should get
#: whichever link lands on a job list, not whichever posting happened to be
#: newest.
TIER_RANK: dict[DerivationTier, int] = {
    DerivationTier.HOST: 0,
    DerivationTier.HOST_AND_SEGMENTS: 0,
    DerivationTier.BOARD: 1,
    DerivationTier.POSTING: 2,
    DerivationTier.NONE: 3,
}


@dataclass(frozen=True)
class CareersLink:
    url: str | None
    tier: DerivationTier


#: Sources whose employer identity lives in the URL path, with the number of
#: path segments to keep after the host.
PATH_SEGMENT_SOURCES: dict[str, int] = {
    "grnhse": 1,  # job-boards.greenhouse.io/ionq
    "lever": 1,  # jobs.lever.co/agile-defense
    "eu_lever": 1,  # jobs.eu.lever.co/everseen
    "ashby": 1,  # jobs.ashbyhq.com/allwyn-corp
    "smartrecruiters": 1,  # jobs.smartrecruiters.com/TimmonsGroup1
    "workday": 1,  # aero.wd5.myworkdayjobs.com/external
    "rippling": 1,  # ats.rippling.com/imagineeer
    "gem": 1,  # jobs.gem.com/bohler-
    "harri": 1,  # harri.com/cavagroup
    "recooty": 1,  # careerspage.io/welo-data
    "dover": 1,  # app.dover.com/apply/<company-uuid>
    "hireology": 1,  # careers.hireology.com/arlodc
    "governmentjobs": 2,  # www.governmentjobs.com/careers/pwcgov
    "jobvite": 1,  # jobs.jobvite.com/pinnaclelive
    "adprecruiting": 1,  # myjobs.adp.com/bessemer
    "gr8people": 0,  # troweprice.gr8people.com - host carries the company
    "silkroad": 2,  # jobs.silkroad.com/JMT/JMTCareers
    "dayforce": 2,  # jobs.dayforcehcm.com/en-US/hendersonco
    "jobscore": 2,  # careers.jobscore.com/careers/cinqcare
    "talentreef": 2,  # apply.jobappnetwork.com/clients/14459
    "csod": 1,  # mathematica.csod.com/ux - host also works, keep it shallow
    "vivahr": 1,  # jobs.avahr.com/40903-rjit-solutions
    "gusto": 0,  # jobs.gusto.com/postings/<company>-<role>-<uuid>: not separable
    "workable": 0,  # jobs.workable.com/view/<id>/...: not separable
    "interfolio": 0,  # apply.interfolio.com/<id>: not separable
    "appone_api": 0,  # apply.appone.com/job/<id>: not separable
    "ourcareerpages": 0,  # jobs.ourcareerpages.com/job/<id>: not separable
    "schoolspring": 0,
}


@dataclass(frozen=True)
class BoardRule:
    """How to rebuild a vendor's job-list URL from a posting URL."""

    keep_params: frozenset[str]
    """Query parameters that identify the employer. Everything else is dropped,
    which is what removes the job id."""

    path: str | None = None
    """Replacement path, when the vendor serves listings from a different one
    than job details."""

    set_params: tuple[tuple[str, str], ...] = ()
    """Extra parameters the listing view needs."""

    path_suffix_replace: tuple[str, str] | None = None
    """Swap the final path element, for vendors that keep a deep path and only
    change the last segment between detail and listing views."""


#: Query-parameter ATSes whose board URL can be rebuilt from the posting URL.
#: These are heuristics against observed URL shapes, so the company website
#: remains the fallback link when one misses.
BOARD_QUERY_SOURCES: dict[str, BoardRule] = {
    # .../recruitment.html?cid=<employer>&ccId=...&jobId=... -> drop jobId
    "adp": BoardRule(keep_params=frozenset({"cid", "ccId"})),
    # .../jobs/ViewJobDetails?job=...&clientkey=<employer> -> .../jobs
    "paycom": BoardRule(keep_params=frozenset({"clientkey"}), path="/v4/ats/web.php/jobs"),
    # /career/JobIntroduction.action?clientId=<employer>&id=... -> CareerHome
    "paycor": BoardRule(keep_params=frozenset({"clientId"}), path="/career/CareerHome.action"),
    # /ta/<employer>.careers?ShowJob=... -> the same page without ShowJob
    "saashr": BoardRule(keep_params=frozenset()),
    # .../requisition.jsp?org=<employer>&cws=...&rid=... -> jobSearch.jsp
    "taleo_rss": BoardRule(
        keep_params=frozenset({"org", "cws"}),
        path_suffix_replace=("requisition.jsp", "jobSearch.jsp"),
    ),
}

#: Sources whose listing URL is the path up to and including the segment after
#: ``/sites/``, plus a fixed suffix. Oracle Cloud Recruiting puts the employer
#: in the host and the site name in the path (``CX``, ``CX_1``, ...), with the
#: job appended as either ``requisitions/job/<id>`` or ``job/<id>``.
SITE_PATH_SOURCES: dict[str, tuple[str, str]] = {
    "oraclecloud": ("sites", "requisitions"),
}

#: Sources where the employer is identified only by a query parameter, so no
#: host- or path-level trim isolates their board.
POSTING_URL_SOURCES: frozenset[str] = frozenset(
    {
        "brassring",  # sjobs.brassring.com/...?partnerid=...&siteid=...
        "hirebridge",  # recruit.hirebridge.com/...?cid=...
        "appone_rss",  # www.appone.com/MainInfoReq.asp?...&B_ID=...
        "njoyn",  # clients.njoyn.com/CORP/xweb/xweb.asp?CLID=...
        "virecruit",  # .../viRecruitSelfApply/RecDefault.aspx?Tag=...
        "brightmove",  # portal.brightmove.com/jb.do?companyGK=...
        "pereless",  # ...index.cfm?cid=...
        "peoplematter",  # api.peoplematter.com/...?jobOpeningId=...
        "csodsaba",  # emea3.recruitmentplatform.com/apply-app/...?jobId=...
        "appvault",  # portal.appvault.com/<co>/job/<id>/...?category=...
        "pageup",  # careers.pageuppeople.com/863/cw/en/job/<id>
        "winocular",  # jobs.pwcs.edu/workspace/wSpace.exe?Action=...
        "oraclepeoplesoft",  # careers.dc.gov/psc/...?JobOpeningId=...
        "salesforce",  # <tenant>.my.salesforce-sites.com/...?jobId=...
        "paradox",  # <tenant>.paradox.ai/co/<Co>/Job?job_id=...
    }
)


def _host_root(parts: tuple[str, str, str, str, str]) -> str:
    scheme, netloc = parts[0], parts[1]
    return urlunsplit((scheme or "https", netloc, "/", "", ""))


def _apply_site_path_rule(split: SplitResult, marker: str, suffix: str) -> str | None:
    """Trim the path to the site root, e.g. ``/.../sites/CX_1/requisitions``."""
    segments = [segment for segment in split.path.split("/") if segment]
    try:
        marker_index = segments.index(marker)
    except ValueError:
        return None
    if marker_index + 1 >= len(segments):
        return None
    kept = segments[: marker_index + 2]
    if suffix:
        kept.append(suffix)
    return urlunsplit((split.scheme, split.netloc, "/" + "/".join(kept), "", ""))


def _apply_board_rule(split: SplitResult, rule: BoardRule) -> str:
    """Rebuild a vendor listing URL from a posting URL."""
    path = split.path
    if rule.path is not None:
        path = rule.path
    elif rule.path_suffix_replace is not None:
        old, new = rule.path_suffix_replace
        if path.endswith(old):
            path = path[: -len(old)] + new

    kept = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=False)
        if key in rule.keep_params
    ]
    kept.extend(rule.set_params)
    return urlunsplit((split.scheme, split.netloc, path, urlencode(kept), ""))


def derive_careers_link(apply_url: str | None, source: str | None) -> CareersLink:
    """Best-effort careers-page URL for the employer behind a posting."""
    if not apply_url:
        return CareersLink(None, DerivationTier.NONE)

    split = urlsplit(apply_url.strip())
    if not split.netloc or split.scheme not in {"http", "https"}:
        return CareersLink(None, DerivationTier.NONE)

    parts = (split.scheme, split.netloc, split.path, split.query, split.fragment)
    key = (source or "").strip().lower()

    site_rule = SITE_PATH_SOURCES.get(key)
    if site_rule is not None:
        trimmed = _apply_site_path_rule(split, *site_rule)
        if trimmed is not None:
            return CareersLink(trimmed, DerivationTier.HOST_AND_SEGMENTS)
        return CareersLink(apply_url, DerivationTier.POSTING)

    board_rule = BOARD_QUERY_SOURCES.get(key)
    if board_rule is not None:
        return CareersLink(_apply_board_rule(split, board_rule), DerivationTier.BOARD)

    if key in POSTING_URL_SOURCES:
        return CareersLink(apply_url, DerivationTier.POSTING)

    segments_to_keep = PATH_SEGMENT_SOURCES.get(key)
    if segments_to_keep:
        segments = [segment for segment in split.path.split("/") if segment]
        if len(segments) >= segments_to_keep:
            kept = "/".join(segments[:segments_to_keep])
            return CareersLink(
                urlunsplit((split.scheme, split.netloc, f"/{kept}/", "", "")),
                DerivationTier.HOST_AND_SEGMENTS,
            )

    # Default, and the explicit choice for sources mapped to zero segments:
    # the host identifies the employer.
    return CareersLink(_host_root(parts), DerivationTier.HOST)


def company_entry_url(careers_url: str | None, company_host: str | None) -> str | None:
    """The page to open when processing an employer, or None if there is none.

    Prefers a careers page on the employer's *own* domain, and falls back to
    their homepage otherwise. A derived careers link usually points at an ATS
    board, and a board shows only what was posted through that one instance:
    Cognizant runs three (``careers.cognizant.com``, ``cognizant.taleo.net``,
    ``tas-cognizant.taleo.net``), Merck two, Accenture two. Treating any one of
    them as the employer's full listing is wrong in exactly the cases - large
    employers - where the full listing matters most.

    Landing on a homepage costs a click to reach the jobs, which on measured
    data is about 85% of postings. That is the price of not mistaking a partial
    list for a complete one.

    Both pipelines call this, and they must agree. They key the visited flag to
    the company host and share one VisitLogger export, so if they opened
    different URLs for the same key, whichever ran first would silently decide
    what "processed" meant and the other page would never open.
    """
    host = normalize_host(company_host or "")
    if not host:
        # Without a homepage there is nothing to fall back to, and keying an
        # ATS host would be refused by the extension anyway - it would open a
        # tab and record nothing.
        return None
    if careers_url and _is_own_domain(careers_url, host):
        return careers_url
    return host_to_url(host)


def _is_own_domain(url: str, host: str) -> bool:
    """Whether a URL sits on the company's own domain rather than a vendor's.

    Used instead of a list of ATS hostnames: a list needs maintaining as
    vendors come and go and would miss the long tail, while this correctly
    keeps white-labelled boards like ``jobs.dish.com`` and rejects
    ``boards.greenhouse.io/spacex``.
    """
    target = normalize_host(urlsplit(url).netloc)
    if not target:
        return False
    return target == host or target.endswith(f".{host}")
