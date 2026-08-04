"""
grapply - job sources

Every source normalises to the same dict so discovery.py never needs to know
where a posting came from:

    id, source, company, title, location, url, posted, description,
    salary, remote (bool | None)

Three families, in descending order of data quality:

  ats         Greenhouse / Lever / Ashby / SmartRecruiters / Workable.
              Public unauthenticated JSON, full descriptions, no blocking.
              Needs a company slug, so coverage is only as wide as the registry.

  board       Seek. The dominant board in NZ and AU - most local roles are
              posted here and nowhere else, so a registry-only scan misses the
              bulk of the market. Search returns a teaser; the full description
              comes from a second per-job call, which is why callers should
              triage on title first and only hydrate survivors.

  aggregator  Arbeitnow, RemoteOK, Remotive, Jobicy, WorkingNomads, Himalayas.
              Remote-worldwide firehoses, no slug needed, descriptions inline.
              Quality is mixed, so the prefilter does the real work.

None of these need a browser. If a future source does, add it behind
render_html() rather than reaching for Playwright at the adapter level.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Any, Callable, Iterable

UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 "
      "Firefox/128.0 grapply-discovery/2.0")

# Per-host politeness. These are public endpoints and we are one job seeker,
# not a crawler farm - keep the request rate obviously reasonable.
#
# The gap is small deliberately. A single global interval also serialises the
# worker pools (every thread queues on the same host lock), so setting it high
# does not just slow the crawl down, it removes the concurrency entirely. This
# rate is a few requests a second to one host for a couple of minutes, which is
# ordinary browsing traffic.
_MIN_INTERVAL = 0.12
_last_hit: dict[str, float] = {}
_throttle_lock = threading.Lock()


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).netloc
    while True:
        with _throttle_lock:
            now = time.monotonic()
            wait = _MIN_INTERVAL - (now - _last_hit.get(host, 0.0))
            if wait <= 0:
                _last_hit[host] = now
                return
        # Sleep outside the lock so other hosts are not blocked behind this one.
        time.sleep(wait)


def http_json(url: str, *, data: bytes | None = None,
              headers: dict[str, str] | None = None,
              timeout: float = 30.0, retries: int = 2) -> Any | None:
    """GET/POST JSON. Returns None rather than raising - a dead source must
    never take the whole scan down."""
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    hdr.update(headers or {})

    for attempt in range(retries + 1):
        _throttle(url)
        try:
            req = urllib.request.Request(url, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 429/5xx are worth another go; 4xx means we asked wrong.
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
    return None


def http_text(url: str, *, headers: dict[str, str] | None = None,
              timeout: float = 30.0, limit: int = 600_000) -> str:
    hdr = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"}
    hdr.update(headers or {})
    _throttle(url)
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(limit).decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return ""


# Some boards post titles with Cyrillic homoglyphs ("С++" with a Cyrillic Es),
# which silently breaks keyword matching. Fold them to ASCII before matching.
_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H",
    "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T",
    "Х": "X", "а": "a", "е": "e", "о": "o", "р": "p",
    "с": "c", "х": "x", "у": "y",
})


def fold(text: str) -> str:
    return (text or "").translate(_HOMOGLYPHS)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = unescape(txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def unmojibake(s: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 upstream.

    Some feeds ship already-broken text - RemoteOK serves an em-dash as the
    literal characters "a EUR ..." rather than the character itself. Text that
    round-trips cleanly back through Latin-1 into valid UTF-8 was mojibake;
    anything else is left alone, so genuine Latin-1 content is not harmed.
    """
    for _ in range(3):          # some feeds are doubly encoded
        if not s or s.isascii():
            return s
        try:
            repaired = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
        if repaired == s:
            return s
        s = repaired
    return s


def _job(**kw: Any) -> dict:
    base = {"id": "", "source": "", "company": "", "title": "", "location": "",
            "url": "", "posted": "", "description": "", "salary": "",
            "remote": None}
    base.update(kw)
    for field in ("title", "company", "location", "description"):
        if isinstance(base.get(field), str):
            base[field] = unmojibake(base[field])
    return base


# ══════════════════════════════════════════════════════════════════════════════
# ATS adapters - need a company slug, give full descriptions
# ══════════════════════════════════════════════════════════════════════════════

def ats_greenhouse(slug: str, name: str) -> list[dict]:
    d = http_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not isinstance(d, dict):
        return []
    return [_job(
        id=f"gh:{slug}:{j.get('id')}", source="greenhouse", company=name,
        title=(j.get("title") or "").strip(),
        location=(j.get("location") or {}).get("name", ""),
        url=j.get("absolute_url", ""),
        posted=j.get("updated_at") or j.get("first_published") or "",
        description=strip_html(j.get("content", "")),
    ) for j in d.get("jobs", [])]


def ats_lever(slug: str, name: str) -> list[dict]:
    d = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        return []
    out = []
    for j in d:
        cats = j.get("categories", {}) or {}
        out.append(_job(
            id=f"lever:{slug}:{j.get('id')}", source="lever", company=name,
            title=(j.get("text") or "").strip(),
            location=cats.get("location", "") or "",
            url=j.get("hostedUrl", ""), posted=str(j.get("createdAt", "")),
            salary=cats.get("commitment", "") or "",
            description=strip_html(
                j.get("descriptionPlain") or j.get("description", "")),
        ))
    return out


def ats_ashby(slug: str, name: str) -> list[dict]:
    d = http_json("https://api.ashbyhq.com/posting-api/job-board/"
                  f"{slug}?includeCompensation=true")
    if not isinstance(d, dict):
        return []
    out = []
    for j in d.get("jobs", []):
        comp = j.get("compensation") or {}
        summary = ""
        if isinstance(comp, dict):
            summary = comp.get("compensationTierSummary") or ""
        out.append(_job(
            id=f"ashby:{slug}:{j.get('id')}", source="ashby", company=name,
            title=(j.get("title") or "").strip(),
            location=j.get("location", "") or "",
            url=j.get("jobUrl", ""), posted=j.get("publishedAt", ""),
            salary=summary, remote=j.get("isRemote"),
            description=strip_html(
                j.get("descriptionHtml") or j.get("descriptionPlain", "")),
        ))
    return out


def ats_smartrecruiters(slug: str, name: str) -> list[dict]:
    """SmartRecruiters splits list and detail. The list call is cheap, so pull
    it all, then hydrate descriptions concurrently - the detail endpoint is the
    only way to get JD text and the prefilter is useless without it."""
    d = http_json("https://api.smartrecruiters.com/v1/companies/"
                  f"{slug}/postings?limit=100")
    if not isinstance(d, dict):
        return []
    stubs = []
    for j in d.get("content", []):
        loc = j.get("location", {}) or {}
        stubs.append((j.get("id"), _job(
            id=f"sr:{slug}:{j.get('id')}", source="smartrecruiters",
            company=name, title=(j.get("name") or "").strip(),
            location=", ".join(x for x in (loc.get("city"),
                                           loc.get("country")) if x),
            url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            posted=j.get("releasedDate", ""),
            remote=bool(loc.get("remote")),
        )))

    def hydrate(pair: tuple[str, dict]) -> dict:
        jid, job = pair
        det = http_json("https://api.smartrecruiters.com/v1/companies/"
                        f"{slug}/postings/{jid}")
        if isinstance(det, dict):
            ad = (det.get("jobAd") or {}).get("sections") or {}
            parts = [(ad.get(k) or {}).get("text", "")
                     for k in ("companyDescription", "jobDescription",
                               "qualifications", "additionalInformation")]
            job["description"] = strip_html("\n\n".join(p for p in parts if p))
        return job

    with ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(hydrate, stubs))


def ats_workable(slug: str, name: str) -> list[dict]:
    d = http_json("https://apply.workable.com/api/v1/widget/accounts/"
                  f"{slug}?details=true")
    if not isinstance(d, dict):
        return []
    out = []
    for j in d.get("jobs", []):
        out.append(_job(
            id=f"wk:{slug}:{j.get('shortcode')}", source="workable",
            company=name, title=(j.get("title") or "").strip(),
            location=", ".join(x for x in (j.get("city"), j.get("country"))
                               if x),
            url=j.get("url" ) or j.get("application_url", ""),
            posted=j.get("published_on", ""),
            remote=j.get("telecommuting"),
            description=strip_html(
                (j.get("description") or "") + "\n" +
                (j.get("requirements") or "")),
        ))
    return out


ATS_ADAPTERS: dict[str, Callable[[str, str], list[dict]]] = {
    "greenhouse":      ats_greenhouse,
    "lever":           ats_lever,
    "ashby":           ats_ashby,
    "smartrecruiters": ats_smartrecruiters,
    "workable":        ats_workable,
}


def fetch_registry(sources: Iterable[dict],
                   on_source: Callable[[str, int], None] | None = None,
                   ) -> list[dict]:
    """Poll every company in the registry. Sources are independent, so run them
    concurrently - a slow board should not stall the rest."""
    srcs = [s for s in sources if s.get("ats") in ATS_ADAPTERS]

    def one(src: dict) -> list[dict]:
        name = src.get("name", src["slug"])
        try:
            got = ATS_ADAPTERS[src["ats"]](src["slug"], name)
        except Exception:                                        # noqa: BLE001
            got = []
        if on_source:
            on_source(name, len(got))
        return got

    jobs: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for got in ex.map(one, srcs):
            jobs.extend(got)
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Seek - the NZ/AU market. Search is cheap, detail is not.
# ══════════════════════════════════════════════════════════════════════════════

SEEK_SITES = {
    "nz": ("https://www.seek.co.nz", "NZ-Main", "NZ", "New Zealand"),
    "au": ("https://www.seek.com.au", "AU-Main", "AU", "Australia"),
}

_SEEK_DETAIL_QUERY = """
query jobDetails($jobId: ID!) {
  jobDetails(id: $jobId) {
    job {
      title
      content(platform: WEB)
      salary { label }
      location { label }
      advertiser { name }
      workTypes { label }
    }
  }
}
"""


def seek_search(keywords: str, *, site: str = "nz", where: str = "",
                max_jobs: int = 200, date_range: int = 31) -> list[dict]:
    """Search Seek. Returns stubs with a teaser, not the full JD - call
    seek_hydrate() on the ones that survive triage.

    date_range is in days. Seek only honours 1/3/7/14/31 and silently ignores
    anything else, handing back the unfiltered list, so the request is rounded
    up to the nearest bucket it accepts.
    """
    base, site_key, cc, default_where = SEEK_SITES.get(site, SEEK_SITES["nz"])
    where = where or default_where
    date_range = min((d for d in (1, 3, 7, 14, 31) if d >= date_range),
                     default=31)
    out: list[dict] = []
    page = 1
    while len(out) < max_jobs and page <= 25:
        q = urllib.parse.urlencode({
            "siteKey": site_key, "sourcesystem": "houston",
            "keywords": keywords, "where": where,
            "page": page, "pageSize": 100, "daterange": date_range,
        })
        d = http_json(f"{base}/api/jobsearch/v5/search?{q}",
                      headers={"Accept": "application/json",
                               "Referer": f"{base}/"})
        rows = (d or {}).get("data") or []
        if not rows:
            break
        for j in rows:
            locs = j.get("locations") or []
            label = locs[0].get("label", "") if locs else ""
            arr = ((j.get("workArrangements") or {}).get("data") or [])
            arrangement = " ".join(
                (a.get("label") or {}).get("text", "") for a in arr).lower()
            out.append(_job(
                id=f"seek:{cc}:{j.get('id')}", source=f"seek-{site}",
                company=j.get("companyName") or
                        (j.get("advertiser") or {}).get("description", ""),
                title=(j.get("title") or "").strip(),
                location=label or where,
                url=f"{base}/job/{j.get('id')}",
                posted=j.get("listingDate", ""),
                salary=j.get("salaryLabel", "") or "",
                remote="remote" in arrangement,
                description=j.get("teaser", "") or "",
            ))
        if len(rows) < 100:
            break
        page += 1
    return out[:max_jobs]


def seek_hydrate(jobs: list[dict], *, site: str = "nz",
                 workers: int = 5,
                 progress: Callable[[int, int], None] | None = None,
                 ) -> list[dict]:
    """Replace the teaser with the real JD. One GraphQL call per job, so only
    ever pass jobs that already survived title-level triage."""
    base, _, cc, _ = SEEK_SITES.get(site, SEEK_SITES["nz"])
    done = 0
    lock = threading.Lock()

    def one(job: dict) -> dict:
        nonlocal done
        jid = job["id"].rsplit(":", 1)[-1]
        payload = json.dumps({
            "operationName": "jobDetails",
            "variables": {"jobId": jid},
            "query": _SEEK_DETAIL_QUERY,
        }).encode()
        d = http_json(f"{base}/graphql", data=payload,
                      headers={"seek-request-brand": "seek",
                               "seek-request-country": cc,
                               "Referer": f"{base}/job/{jid}"})
        j = (((d or {}).get("data") or {}).get("jobDetails") or {}).get("job")
        if isinstance(j, dict):
            body = strip_html(j.get("content", ""))
            if body:
                job["description"] = body
            if not job.get("salary"):
                job["salary"] = (j.get("salary") or {}).get("label", "") or ""
        with lock:
            done += 1
            if progress:
                progress(done, len(jobs))
        return job

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, jobs))


# ══════════════════════════════════════════════════════════════════════════════
# Remote aggregators - no slug needed, worldwide, descriptions inline
# ══════════════════════════════════════════════════════════════════════════════

def agg_arbeitnow() -> list[dict]:
    d = http_json("https://www.arbeitnow.com/api/job-board-api")
    return [_job(
        id=f"arbeitnow:{j.get('slug')}", source="arbeitnow",
        company=j.get("company_name", ""), title=(j.get("title") or "").strip(),
        location=j.get("location", ""), url=j.get("url", ""),
        posted=str(j.get("created_at", "")), remote=bool(j.get("remote")),
        description=strip_html(j.get("description", "")),
    ) for j in (d or {}).get("data", [])]


def agg_remoteok() -> list[dict]:
    d = http_json("https://remoteok.com/api")
    if not isinstance(d, list):
        return []
    out = []
    for j in d:
        # The first element is a legal/attribution notice, not a posting.
        if not isinstance(j, dict) or not j.get("id"):
            continue
        lo, hi = j.get("salary_min"), j.get("salary_max")
        out.append(_job(
            id=f"remoteok:{j.get('id')}", source="remoteok",
            company=j.get("company", ""),
            title=(j.get("position") or "").strip(),
            location=j.get("location", "") or "Remote",
            url=j.get("url") or j.get("apply_url", ""),
            posted=j.get("date", ""), remote=True,
            salary=f"{lo}-{hi}" if lo and hi else "",
            description=strip_html(j.get("description", "")),
        ))
    return out


def agg_remotive() -> list[dict]:
    d = http_json("https://remotive.com/api/remote-jobs?limit=200")
    return [_job(
        id=f"remotive:{j.get('id')}", source="remotive",
        company=j.get("company_name", ""), title=(j.get("title") or "").strip(),
        location=j.get("candidate_required_location", "") or "Remote",
        url=j.get("url", ""), posted=j.get("publication_date", ""),
        salary=j.get("salary", "") or "", remote=True,
        description=strip_html(j.get("description", "")),
    ) for j in (d or {}).get("jobs", [])]


def agg_jobicy() -> list[dict]:
    d = http_json("https://jobicy.com/api/v2/remote-jobs?count=100")
    return [_job(
        id=f"jobicy:{j.get('id')}", source="jobicy",
        company=j.get("companyName", ""),
        title=(j.get("jobTitle") or "").strip(),
        location=j.get("jobGeo", "") or "Remote", url=j.get("url", ""),
        posted=j.get("pubDate", ""), remote=True,
        salary=str(j.get("annualSalaryMin") or ""),
        description=strip_html(j.get("jobDescription")
                               or j.get("jobExcerpt", "")),
    ) for j in (d or {}).get("jobs", [])]


def agg_workingnomads() -> list[dict]:
    d = http_json("https://www.workingnomads.com/api/exposed_jobs/")
    if not isinstance(d, list):
        return []
    return [_job(
        id=f"wnomads:{(j.get('url') or '').rstrip('/').rsplit('/', 1)[-1]}",
        source="workingnomads", company=j.get("company_name", ""),
        title=(j.get("title") or "").strip(),
        location=j.get("location", "") or "Remote", url=j.get("url", ""),
        posted=j.get("pub_date", ""), remote=True,
        description=strip_html(j.get("description", "")),
    ) for j in d]


def agg_himalayas() -> list[dict]:
    out: list[dict] = []
    for offset in (0, 100, 200):
        d = http_json(
            f"https://himalayas.app/jobs/api?limit=100&offset={offset}")
        rows = (d or {}).get("jobs") or []
        if not rows:
            break
        for j in rows:
            locs = j.get("locationRestrictions") or []
            out.append(_job(
                id=f"himalayas:{j.get('guid')}", source="himalayas",
                company=j.get("companyName", ""),
                title=(j.get("title") or "").strip(),
                location=", ".join(locs) if locs else "Remote",
                url=j.get("applicationLink", ""), posted=str(j.get("pubDate", "")),
                remote=True,
                salary=(f"{j.get('minSalary')}-{j.get('maxSalary')}"
                        if j.get("minSalary") else ""),
                description=strip_html(j.get("description")
                                       or j.get("excerpt", "")),
            ))
    return out


AGGREGATORS: dict[str, Callable[[], list[dict]]] = {
    "arbeitnow":     agg_arbeitnow,
    "remoteok":      agg_remoteok,
    "remotive":      agg_remotive,
    "jobicy":        agg_jobicy,
    "workingnomads": agg_workingnomads,
    "himalayas":     agg_himalayas,
}


def fetch_aggregators(names: Iterable[str] | None = None,
                      on_source: Callable[[str, int], None] | None = None,
                      ) -> list[dict]:
    wanted = [n for n in (names or AGGREGATORS) if n in AGGREGATORS]

    def one(n: str) -> list[dict]:
        try:
            got = AGGREGATORS[n]()
        except Exception:                                        # noqa: BLE001
            got = []
        if on_source:
            on_source(n, len(got))
        return got

    jobs: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for got in ex.map(one, wanted):
            jobs.extend(got)
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Registry growth - find a company's ATS instead of guessing its slug
# ══════════════════════════════════════════════════════════════════════════════

_BOARD_PATTERNS = [
    (re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?"
                r"([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I),
     "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I), "ashby"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_-]+)",
                re.I), "ashby"),
    (re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I), "workable"),
    (re.compile(r"jobs\.smartrecruiters\.com/([a-z0-9_-]+)", re.I),
     "smartrecruiters"),
]

# Guessed slugs 404 far more often than they hit, so sniffing the careers page
# is the primary route and guessing is only the fallback.
_CAREERS_PATHS = ("", "/careers", "/careers/", "/en/careers", "/jobs",
                  "/about/careers", "/company/careers", "/join-us")


def discover_ats(site: str, name: str = "") -> list[dict]:
    """Sniff a company site for an embedded ATS board. Returns registry rows
    ready to append to sources.json."""
    if not site.startswith("http"):
        site = "https://" + site
    site = site.rstrip("/")
    found: dict[tuple[str, str], dict] = {}

    for path in _CAREERS_PATHS:
        html = http_text(site + path, limit=500_000)
        if not html:
            continue
        for pat, ats in _BOARD_PATTERNS:
            for m in pat.finditer(html):
                slug = m.group(1)
                if slug.lower() in ("embed", "job_board", "www", "api"):
                    continue
                found[(ats, slug)] = {
                    "ats": ats, "slug": slug,
                    "name": name or urllib.parse.urlparse(site).netloc,
                }
        if found:
            break

    # Verify each hit actually returns postings before offering it.
    verified = []
    for row in found.values():
        try:
            if ATS_ADAPTERS[row["ats"]](row["slug"], row["name"]):
                verified.append(row)
        except Exception:                                        # noqa: BLE001
            pass
    return verified


def validate_source(row: dict) -> tuple[bool, int]:
    fn = ATS_ADAPTERS.get(row.get("ats", ""))
    if not fn:
        return False, 0
    try:
        got = fn(row["slug"], row.get("name", row["slug"]))
    except Exception:                                            # noqa: BLE001
        return False, 0
    return bool(got), len(got)
