"""
grapply - job discovery

Finds postings worth applying to, instead of waiting for you to find them.

Search runs in phases, in priority order. The default is the shape most job
hunts actually have: everything local first, then remote-worldwide as the
fallback. Each phase decides its own sources and its own location gate, so
"remote" means remote in the phase that wants it and is irrelevant in the one
that does not.

  phase 1  New Zealand   Seek NZ + the company registry
  phase 2  Remote        remote aggregators + the registry

Scoring stays two-stage, because running an LLM over every posting is wasteful
when Seek alone returns hundreds:

  stage 1  keyword prefilter, local, milliseconds, fully explainable
  stage 2  LLM fit score, then a comparative ranking pass, survivors only

Seen postings are remembered but NOT hidden. Hiding them was why a rescan kept
showing an empty or week-old list: everything had been seen once, so nothing
survived. A posting now disappears only when you dismiss or apply to it.

  python -m companion.discovery --validate       # check the registry
  python -m companion.discovery --prefilter-only # no LLM, fast triage
  python -m companion.discovery --rank           # full run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from . import sources as src_mod
except ImportError:                                              # direct run
    import sources as src_mod                                    # type: ignore

STATE_PATH   = Path("~/.grapply/discovery_state.json").expanduser()
LAST_PATH    = Path("~/.grapply/discovery_last.json").expanduser()
SOURCES_PATH = Path("~/.grapply/sources.json").expanduser()
CONFIG_PATH  = Path("~/.grapply/discovery_config.json").expanduser()


# ══════════════════════════════════════════════════════════════════════════════
# Candidate profile - hard gates and weighted signals
# ══════════════════════════════════════════════════════════════════════════════
# Every result reports which terms matched, so thresholds can be calibrated
# against real runs instead of guessed.

TITLE_REJECT = (
    "intern", "internship", "graduate", "new grad", "junior", "trainee",
    "apprentice", "student", "placement", "co-op", "co op",
)

# The NZ market is far too small to gate on C++ alone - that rejected roughly
# 97% of everything fetched. C#/.NET (Framecad, AWS-backed services) and Python
# (SAP tooling, current work) are both real, shipped experience, so a posting
# qualifies on any of the three. Priority order is reflected in the weights
# below, not by excluding the other two here.
REQUIRE_ANY = ("c++", "cpp", "c/c++", "c#", "csharp", ".net", "dotnet",
               "python")

# A JD body mentioning C++ is not enough - quant firms name it in trader and
# analyst postings too. The title has to describe an engineering role.
TITLE_REQUIRE_ANY = (
    "engineer", "developer", "programmer", "software", "architect",
    "sre", "devops", "lead", "leader",
)

# Some postings carry a senior-sounding title but open with "Junior ...".
BODY_LEVEL_REJECT = (
    "seeking a junior", "looking for a junior", "hiring a junior",
    "junior quantitative", "junior software", "junior developer",
    "junior engineer", "graduate programme", "graduate program",
    "entry-level", "entry level position",
)

TITLE_REJECT_ROLE = (
    "trader", "analyst", "researcher", "recruiter", "sales", "account",
    "marketing", "counsel", "accountant", "buyer", "planner", "technician",
    "quantitative trader", "hr ",
)

HARD_BLOCK = (
    "security clearance", "ts/sci", "must be a u.s. citizen",
    "must be a us citizen", "us citizenship is required",
    "citizens only", "itar", "polygraph",
)

# US aerospace and defence firms paste ITAR boilerplate into every posting,
# including Auckland roles staffed by locals. The block only bites when the role
# is genuinely US-located; elsewhere it becomes a note to verify.
NON_US_LOCATION = (
    "new zealand", "auckland", "wellington", "christchurch", "nz",
    "australia", "sydney", "melbourne", "brisbane", "perth",
    "netherlands", "amsterdam", "germany", "berlin", "munich", "london",
    "united kingdom", "singapore", "hong kong", "japan", "canada", "india",
)

NZ_TERMS = ("new zealand", "auckland", "wellington", "christchurch",
            "hamilton", "tauranga", "dunedin", "palmerston", "napier", "nz")

REMOTE_TERMS = ("remote", "anywhere", "worldwide", "global", "distributed",
                "work from home", "wfh", "fully remote", "remote-first")

WEIGHTS: dict[str, tuple[int, tuple[str, ...]]] = {
    # Language buckets are weighted by preference, not by capability: C++ is
    # the deepest and best-evidenced, C#/.NET is real production experience,
    # Python is genuine but more supporting-cast. A role scores on whichever it
    # asks for, and a C++ role still outranks an equivalent Python one.
    "core_cpp": (24, (
        "c++", "c++11", "c++14", "c++17", "c++20", "c++23", "modern c++",
        "stl", "template", "raii", "object-oriented", "object oriented",
    )),
    "core_dotnet": (18, (
        "c#", "csharp", ".net", "dotnet", ".net core", "asp.net", "wpf",
        "entity framework", "nuget", "xamarin", "blazor",
    )),
    "core_python": (14, (
        "python", "python3", "django", "flask", "fastapi", "numpy", "pandas",
        "pytest", "asyncio",
    )),
    "systems": (24, (
        "embedded linux", "embedded", "device driver", "driver development",
        "kernel", "rtos", "qnx", "real-time", "real time", "bare metal",
        "multithread", "multi-threaded", "concurrency", "lock-free",
        "memory model", "performance", "optimisation", "optimization",
        "profiling", "latency", "throughput", "linux internals", "posix",
        "cross-compil*", "buildroot", "sanitizer", "valgrind",
    )),
    "domain": (20, (
        "automotive", "radar", "lidar", "sensor fusion", "perception",
        "adas", "autonomous", "autonomy", "localisation", "localization",
        "navigation", "gnss", "gps", "mapping", "geospatial", "infotainment",
        "audio", "gstreamer", "media pipeline", "codec", "dsp", "streaming",
        "webrtc", "robotics", "motion control", "machine control",
        "industrial", "mining", "marine", "can bus", "ethercat", "canopen",
        "flight software", "avionics", "satellite", "spacecraft", "aerospace",
        "market data", "low latency", "low-latency", "trading", "hft",
        "exchange", "order book", "quantitative",
    )),
    "tools": (14, (
        "qt", "opengl", "vulkan", "cuda", "openmp", "protobuf", "grpc",
        "cmake", "docker", "python", "rust", "linux", "git", "gerrit",
        "ci/cd", "gitlab", "github actions", "postgresql", "redis", "sdl",
        "ffmpeg", "webassembly", "emscripten",
    )),
    "seniority": (18, (
        "senior", "staff", "principal", "lead", "architect", "expert",
        "specialist",
    )),
}

# Genuine gaps. A keyword match that dies in the first technical call is worse
# than no match at all.
PENALTIES: dict[str, int] = {
    "unreal engine": 12, "unity": 10, "game engine": 10, "gameplay": 8,
    "animation": 5, "shader": 4,
    "yocto": 5, "bitbake": 5, "freertos": 4, "zephyr": 3,
    "microcontroller": 5, "bare-metal mcu": 6,
    "nmea": 3, "iec 62304": 4, "fpga": 4, "verilog": 5, "vhdl": 5,
    "php": 6, "ruby": 6, "salesforce": 10, "sap abap": 0,
}


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SOURCES: list[dict[str, str]] = [
    {"ats": "greenhouse", "slug": "rocketlab",     "name": "Rocket Lab"},
    {"ats": "greenhouse", "slug": "eqvilentjobs",  "name": "Eqvilent"},
    {"ats": "greenhouse", "slug": "imc",           "name": "IMC Trading"},
    {"ats": "greenhouse", "slug": "janestreet",    "name": "Jane Street"},
    {"ats": "greenhouse", "slug": "dawnaerospace", "name": "Dawn Aerospace"},
]

# Seek is queried per keyword, so keep the list short and high-signal - each
# term is a full paginated search.
DEFAULT_CONFIG: dict[str, Any] = {
    "phases": [
        {
            "name": "New Zealand",
            "enabled": True,
            "gate": "nz",
            # Searching "c++" alone is too narrow: boards tokenise the plus
            # signs unpredictably and plenty of matching roles never put it in
            # the title. Cast wide on job titles and let the prefilter enforce
            # the C++ requirement against the full JD - recall goes up, and
            # precision is unchanged because the gate is downstream.
            "keywords": ["senior software engineer", "senior software developer",
                         "c++", "c# .net", "python developer",
                         "embedded software", "firmware"],
            "seek_sites": ["nz"],
            "aggregators": [],
            # Indeed needs a headed, signed-in browser (page 2+ is gated behind
            # login), which the render layer handles. Seek is deliberately not
            # here: it server-renders and its JSON API returns more, faster.
            "browser_profiles": ["indeed-nz"],
            "browser_pages": 6,
            "use_registry": True,
            "max_jobs": 400,
        },
        {
            "name": "Remote worldwide",
            "enabled": True,
            "gate": "remote",
            "keywords": ["c++", "senior software engineer"],
            "seek_sites": [],
            "aggregators": ["arbeitnow", "remoteok", "remotive", "jobicy",
                            "workingnomads", "himalayas"],
            "browser_profiles": [],
            "use_registry": True,
            "max_jobs": 400,
        },
    ],
    "prefilter_min": 45.0,
    "llm_limit": 25,
    # Seek detail calls per phase. This is a total now that stubs are
    # de-duplicated across keywords, not a per-keyword budget - set it below
    # the unique candidate count and you silently throw away good matches.
    "hydrate_limit": 320,
    "browser_engine": "rotate",
    "browser_device": "desktop",
    "headless": True,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            if isinstance(cfg, dict) and cfg.get("phases"):
                merged = dict(DEFAULT_CONFIG)
                merged.update(cfg)
                return merged
        except ValueError:
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def load_sources() -> list[dict[str, str]]:
    if SOURCES_PATH.exists():
        try:
            data = json.loads(SOURCES_PATH.read_text())
            if isinstance(data, list) and data:
                return data
        except ValueError:
            pass
    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_PATH.write_text(json.dumps(DEFAULT_SOURCES, indent=2))
    return DEFAULT_SOURCES


def save_sources(rows: list[dict]) -> None:
    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_PATH.write_text(json.dumps(rows, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# State - remember, but do not hide
# ══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text())
            if isinstance(st, dict):
                st.setdefault("seen", {})
                st.setdefault("dismissed", {})
                st.setdefault("applied", {})
                return st
        except ValueError:
            pass
    return {"seen": {}, "dismissed": {}, "applied": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def dismiss(job_id: str) -> None:
    st = _load_state()
    st["dismissed"][job_id] = datetime.now(timezone.utc).isoformat()
    _save_state(st)


def mark_applied(job_id: str) -> None:
    st = _load_state()
    st["applied"][job_id] = datetime.now(timezone.utc).isoformat()
    _save_state(st)


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 - local prefilter
# ══════════════════════════════════════════════════════════════════════════════

_TERM_RX: dict[str, re.Pattern] = {}


def _has(blob: str, term: str) -> bool:
    """Whole-term match, not substring.

    Plain `in` is badly wrong here: "unity" fires on "opportunity", "git" on
    "legitimate", "rust" on "trust". Every posting picked up a spurious -10 for
    a game engine it never mentioned. Terms are matched on non-alphanumeric
    boundaries instead, so "c++" and "ci/cd" still work. A trailing "*" marks a
    deliberate prefix, e.g. "cross-compil*" matching "cross-compilation".
    """
    rx = _TERM_RX.get(term)
    if rx is None:
        prefix = term.endswith("*")
        raw = term[:-1] if prefix else term
        core = re.escape(raw)
        # A boundary only makes sense against an alphanumeric edge. "c++" ends
        # in '+', so demanding a non-alphanumeric after it would reject
        # "c++17" - which is exactly how most postings write it.
        head = r"(?<![a-z0-9])" if raw[:1].isalnum() else ""
        tail = r"(?![a-z0-9])" if (raw[-1:].isalnum() and not prefix) else ""
        rx = re.compile(rf"{head}{core}{tail}")
        _TERM_RX[term] = rx
    return rx.search(blob) is not None


def _any(blob: str, terms: Iterable[str]) -> bool:
    return any(_has(blob, t) for t in terms)


def _location_ok(job: dict, gate: str) -> tuple[bool, str]:
    """Phase-aware location gate. Returns (ok, reason_if_not)."""
    loc = src_mod.fold(job.get("location") or "").lower()
    body = src_mod.fold(job.get("description") or "").lower()[:3000]
    is_remote = bool(job.get("remote")) or _any(loc, REMOTE_TERMS)

    if gate == "nz":
        if _any(loc, NZ_TERMS):
            return True, ""
        # A worldwide-remote role is reachable from NZ, so it belongs here too.
        if is_remote and _any(f"{loc} {body}",
                              ("worldwide", "anywhere", "global",
                               "new zealand")):
            return True, ""
        return False, f"not NZ-reachable: {job.get('location') or '?'}"

    if gate == "remote":
        if is_remote or _any(body[:1500], REMOTE_TERMS):
            return True, ""
        return False, f"not remote: {job.get('location') or '?'}"

    return True, ""


def prefilter(job: dict, gate: str = "") -> dict:
    """Score 0-100 on keywords alone. No LLM, fully explainable."""
    title = src_mod.fold(job["title"]).lower()
    loc   = src_mod.fold(job.get("location") or "").lower()
    body  = src_mod.fold(job.get("description", "")).lower()
    blob  = f"{title}\n{loc}\n{body}"

    reject: list[str] = []
    notes: list[str] = []
    non_us = any(t in loc for t in NON_US_LOCATION)

    if _any(title, TITLE_REJECT):
        reject.append("title looks junior/intern")
    if not _any(title, TITLE_REQUIRE_ANY):
        reject.append(f"title is not an engineering role: {job['title']}")
    if _any(title, TITLE_REJECT_ROLE):
        reject.append(f"non-engineering role type: {job['title']}")

    opening = body[:1200]
    for t in BODY_LEVEL_REJECT:
        if _has(opening, t):
            reject.append(f"body advertises a junior/entry role ('{t}')")
            break

    if not _any(blob, REQUIRE_ANY):
        reject.append("no C/C++ signal")

    if gate:
        ok, why = _location_ok(job, gate)
        if not ok:
            reject.append(why)

    for t in HARD_BLOCK:
        if _has(blob, t):
            if non_us:
                notes.append(
                    f"'{t}' appears in the text, but the role is located in "
                    f"{job.get('location')} - likely US template boilerplate. "
                    "Verify.")
            else:
                reject.append(f"hard block: {t}")
            break

    hits: dict[str, list[str]] = {}
    score = 0.0
    for bucket, (weight, terms) in WEIGHTS.items():
        target = title if bucket == "seniority" else blob
        matched = [t.rstrip("*") for t in terms if _has(target, t)]
        if matched:
            hits[bucket] = matched
            # Saturating: 1 hit earns 55% of the weight, 4+ earns all of it.
            frac = min(1.0, 0.55 + 0.15 * (len(matched) - 1))
            score += weight * frac

    pen: list[str] = []
    for term, cost in PENALTIES.items():
        if cost and _has(blob, term):
            score -= cost
            pen.append(f"{term} (-{cost})")

    return {
        "prefilter_score": round(max(0.0, min(100.0, score)), 1),
        "hits": hits, "penalties": pen, "notes": notes, "reject": reject,
    }


def triage_title(job: dict) -> bool:
    """Cheap gate for deciding whether a posting is worth a detail fetch.

    Seek search returns a teaser, and hydrating every hit would be hundreds of
    calls. Judge on title and teaser first: reject what is obviously wrong, keep
    anything plausible - the real prefilter runs afterwards on the full JD.
    """
    title = src_mod.fold(job.get("title", "")).lower()
    if _any(title, TITLE_REJECT):
        return False
    if _any(title, TITLE_REJECT_ROLE):
        return False
    if not _any(title, TITLE_REQUIRE_ANY):
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Dedupe - the same role often appears on Seek and on the company's own board
# ══════════════════════════════════════════════════════════════════════════════

_NOISE = re.compile(r"[^a-z0-9+ ]+")


def _dedupe_key(job: dict) -> tuple[str, str]:
    title = _NOISE.sub(" ", src_mod.fold(job.get("title", "")).lower())
    title = re.sub(r"\b(senior|snr|sr|junior|jnr|lead|principal|staff)\b",
                   " ", title)
    company = _NOISE.sub(" ", src_mod.fold(job.get("company", "")).lower())
    return (" ".join(title.split()), " ".join(company.split()))


def dedupe(jobs: list[dict]) -> list[dict]:
    """Keep the richest copy of each role: the one with the longest JD, since
    that is what the prefilter and the LLM both read."""
    best: dict[tuple[str, str], dict] = {}
    for j in jobs:
        k = _dedupe_key(j)
        if not k[0]:
            continue
        cur = best.get(k)
        if cur is None or len(j.get("description", "")) > len(
                cur.get("description", "")):
            if cur is not None:
                j.setdefault("also_on", []).extend(
                    [cur.get("source", "")] + cur.get("also_on", []))
            best[k] = j
        else:
            cur.setdefault("also_on", []).append(j.get("source", ""))
    for j in best.values():
        if j.get("also_on"):
            # Only worth showing when the duplicate came from a different
            # source - "also on seek-nz" for a Seek posting is just noise.
            others = {s for s in j["also_on"] if s and s != j.get("source")}
            if others:
                j["also_on"] = sorted(others)
            else:
                j.pop("also_on", None)
    return list(best.values())


# ══════════════════════════════════════════════════════════════════════════════
# Gathering
# ══════════════════════════════════════════════════════════════════════════════

def gather_phase(phase: dict, cfg: dict, registry: list[dict],
                 log: Callable[[str], None],
                 registry_jobs: list[dict] | None = None) -> list[dict]:
    """Fetch every source a phase asks for. Never raises for one bad source.

    registry_jobs lets the caller poll the company registry once and share it
    across phases - the ATS results are identical either way, and every phase
    re-polling them is pure duplicate traffic.
    """
    jobs: list[dict] = []
    gate = phase.get("gate", "")

    if phase.get("use_registry", True) and registry:
        if registry_jobs is None:
            registry_jobs = src_mod.fetch_registry(
                registry, on_source=lambda n, c: log(f"    registry {n}: {c}"))
        got = [dict(j) for j in registry_jobs]
        log(f"  registry: {len(got)}")
        jobs.extend(got)

    for site in phase.get("seek_sites", []):
        # Collect every keyword's hits first and de-duplicate by posting id
        # before hydrating. Keywords overlap heavily - a senior C++ embedded
        # role matches four of them - and hydration is one HTTP call per
        # posting, so deduplicating first is the difference between ~450 calls
        # and ~150 for the same result.
        per_kw: list[list[dict]] = []
        seen_ids: set[str] = set()
        for kw in phase.get("keywords", []):
            try:
                stubs = src_mod.seek_search(
                    kw, site=site, max_jobs=phase.get("max_jobs", 300))
            except Exception as exc:                             # noqa: BLE001
                log(f"  seek-{site} '{kw}': failed ({exc})")
                continue
            keep = [j for j in stubs if triage_title(j)]
            fresh = [j for j in keep if j["id"] not in seen_ids]
            seen_ids.update(j["id"] for j in fresh)
            log(f"  seek-{site} '{kw}': {len(stubs)} found, "
                f"{len(keep)} past triage, {len(fresh)} new")
            per_kw.append(fresh)

        # Interleave rather than concatenate. Seek returns by relevance, so
        # every keyword's best hits should get budget - concatenating lets the
        # broadest keyword consume it all before the specific ones are reached.
        pool: list[dict] = []
        for i in range(max((len(k) for k in per_kw), default=0)):
            for lst in per_kw:
                if i < len(lst):
                    pool.append(lst[i])

        todo = pool[: cfg.get("hydrate_limit", 320)]
        if todo:
            log(f"  seek-{site}: hydrating {len(todo)} unique postings")
            todo = src_mod.seek_hydrate(todo, site=site)
        jobs.extend(todo)

    aggs = phase.get("aggregators", [])
    if aggs:
        got = src_mod.fetch_aggregators(
            aggs, on_source=lambda n, c: log(f"    {n}: {c}"))
        log(f"  aggregators: {len(got)}")
        jobs.extend(got)

    profiles = phase.get("browser_profiles", [])
    if profiles:
        try:
            from . import render                                 # noqa: PLC0415
        except ImportError:
            try:
                import render                                    # type: ignore
            except ImportError:
                render = None                                    # type: ignore
        if render is None or not render.available():
            log("  browser profiles skipped (playwright not installed)")
        else:
            before = len(jobs)
            # Browser sources are minutes per keyword, and boards run their own
            # relevance ranking, so a second near-synonym mostly re-returns the
            # first one's results. One keyword by default; raise it only if a
            # site genuinely partitions its index by query.
            for kw in phase.get("keywords", [])[:phase.get("browser_keywords", 1)]:
                try:
                    got = render.fetch_profiles(
                        profiles, kw, max_jobs=phase.get("max_jobs", 100),
                        max_pages=phase.get("browser_pages", 4),
                        headless=cfg.get("headless", True),
                        engine=cfg.get("browser_engine", "rotate"),
                        device=cfg.get("browser_device", "desktop"))
                except Exception as exc:                         # noqa: BLE001
                    log(f"  browser '{kw}': failed ({exc})")
                    continue
                log(f"  browser '{kw}': {len(got)}")
                jobs.extend(got)
            if len(jobs) == before:
                log("  browser returned nothing - JSON sources still apply")

    for j in jobs:
        j.setdefault("phase", phase.get("name", gate))
    return jobs


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 - LLM scoring and comparative ranking
# ══════════════════════════════════════════════════════════════════════════════

def _ai_bits():
    try:
        from . import ai_client as ai_module, analyzer, config
    except ImportError:                                          # direct run
        import ai_client as ai_module, analyzer, config          # type: ignore
    return ai_module, analyzer, config


def llm_score(jobs: list[dict], verbose: bool = True,
              progress: Callable[[int, int], None] | None = None,
              ) -> list[dict]:
    ai_module, analyzer, config = _ai_bits()
    cfg    = config.load()
    resume = config.load_resume_text(cfg)
    if not resume.strip():
        print("!! no resume text - set [resume].path in ~/.grapply/config.toml",
              file=sys.stderr)
        return jobs
    ai = ai_module.AIClient(cfg["ai"])

    for i, job in enumerate(jobs, 1):
        if verbose:
            print(f"  [{i}/{len(jobs)}] {job['company']} - {job['title'][:52]}",
                  file=sys.stderr)
        if progress:
            progress(i, len(jobs))
        try:
            res = analyzer.score_job_fit(job.get("description", ""), resume, ai)
        except Exception as exc:                                 # noqa: BLE001
            res = {"error": str(exc), "score": 0}
        job["llm"] = res
        job["llm_score"] = float(res.get("score", 0) or 0)
    return jobs


RANK_SYSTEM = (
    "You are a blunt technical recruiter ranking roles for ONE candidate. "
    "You must produce a STRICT total ordering with no ties. Discriminate hard: "
    "the difference between rank 1 and rank 10 should be obvious from your "
    "reasons. Penalise roles that are not primarily software engineering, and "
    "roles whose core domain the candidate has never worked in. Reward roles "
    "where the candidate's strongest evidence maps onto the core duty. "
    "Reply with ONLY a JSON array, no prose, no markdown fence:\n"
    '[{"rank":1,"id":"<id>","verdict":"strong|worth it|marginal|skip",'
    '"why":"<one sentence, max 22 words>"}]'
)


def rank_candidates(jobs: list[dict], verbose: bool = True) -> list[dict]:
    """Rank the shortlist against each other rather than in isolation.

    Scoring postings alone compresses everything into 8-9. Ranking comparatively
    forces real separation, which is what you need to decide where to spend a
    limited number of applications.
    """
    if len(jobs) < 2:
        return jobs
    ai_module, _, config = _ai_bits()
    cfg    = config.load()
    resume = config.load_resume_text(cfg)
    ai     = ai_module.AIClient(cfg["ai"])

    rows = []
    for j in jobs:
        llm = j.get("llm", {}) or {}
        matched = "; ".join(
            f"{k}:{','.join(v[:5])}" for k, v in (j.get("hits") or {}).items())
        rows.append(
            f"id={j['id']}\n"
            f"  title={j['title']}\n"
            f"  company={j['company']}  location={j.get('location','')}\n"
            f"  matched={matched}\n"
            f"  llm_skills={llm.get('matching_skills','')}\n"
            f"  llm_gaps={llm.get('gaps','')}")

    user = (f"CANDIDATE RESUME:\n{resume[:6000]}\n\n"
            f"ROLES TO RANK - there are exactly {len(jobs)}. Your JSON array "
            f"MUST contain exactly {len(jobs)} objects, ranks 1..{len(jobs)}, "
            f"every id used once. Do not truncate the list.\n\n"
            + "\n\n".join(rows))

    if verbose:
        print(f"  ranking {len(jobs)} roles comparatively...", file=sys.stderr)
    try:
        raw = ai.generate(RANK_SYSTEM, user, _endpoint="analyze")
        m = re.search(r"\[.*\]", raw, re.S)
        order = json.loads(m.group(0)) if m else []
    except Exception as exc:                                     # noqa: BLE001
        print(f"  !! ranking failed: {exc}", file=sys.stderr)
        return jobs

    by_id = {j["id"]: j for j in jobs}
    for row in order:
        j = by_id.get(row.get("id", ""))
        if j:
            j["rank"]    = row.get("rank")
            j["verdict"] = row.get("verdict", "")
            j["why"]     = row.get("why", "")
    ranked   = sorted((j for j in jobs if j.get("rank")),
                      key=lambda x: x["rank"])
    unranked = [j for j in jobs if not j.get("rank")]
    return ranked + unranked


# ══════════════════════════════════════════════════════════════════════════════
# Scan
# ══════════════════════════════════════════════════════════════════════════════

def run_scan(prefilter_min: float | None = None, limit: int | None = None,
             do_llm: bool = True, do_rank: bool = True,
             include_seen: bool = True,
             progress: Callable[[str, int, int], None] | None = None,
             ) -> dict:
    """Full scan across every enabled phase, persisted to disk.

    include_seen defaults to True: a posting you have not acted on is still a
    live opportunity, and hiding it was what made rescans look empty. Dismissed
    and applied postings are always excluded.
    """
    def tick(stage: str, done: int = 0, total: int = 0) -> None:
        if progress:
            try:
                progress(stage, done, total)
            except Exception:                                    # noqa: BLE001
                pass

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    cfg = load_config()
    if prefilter_min is None:
        prefilter_min = float(cfg.get("prefilter_min", 45.0))
    if limit is None:
        limit = int(cfg.get("llm_limit", 25))

    registry = load_sources()
    state    = _load_state()
    seen     = state["seen"]
    hidden   = set(state["dismissed"]) | set(state["applied"])

    phases = [p for p in cfg.get("phases", []) if p.get("enabled", True)]
    all_jobs: list[dict] = []
    survivors: list[dict] = []
    rejected = 0
    per_phase: list[dict] = []

    # Poll the registry once and share it - the phases differ in how they gate
    # results, not in what the ATS boards return.
    registry_jobs: list[dict] | None = None
    if registry and any(p.get("use_registry", True) for p in phases):
        tick("fetching: registry", 0, len(phases))
        registry_jobs = src_mod.fetch_registry(registry)
        log(f"registry: {len(registry_jobs)} postings from "
            f"{len(registry)} companies")

    for idx, phase in enumerate(phases, 1):
        name = phase.get("name", f"phase {idx}")
        tick(f"fetching: {name}", idx - 1, len(phases))
        log(f"\n[{idx}/{len(phases)}] {name}")

        raw = gather_phase(phase, cfg, registry, log, registry_jobs)
        raw = [j for j in raw if j["id"] not in hidden]
        all_jobs.extend(raw)

        kept = []
        for j in raw:
            j.update(prefilter(j, phase.get("gate", "")))
            if j["reject"]:
                rejected += 1
                continue
            if j["prefilter_score"] >= prefilter_min:
                kept.append(j)
        log(f"  -> {len(kept)} passed prefilter (>= {prefilter_min})")
        per_phase.append({"name": name, "fetched": len(raw), "kept": len(kept)})
        survivors.extend(kept)

    # Dedupe across phases and sources, then rank by the cheap score first so
    # the LLM budget goes to the most promising postings.
    before = len(survivors)
    survivors = dedupe(survivors)
    if before != len(survivors):
        log(f"\ndeduped {before} -> {len(survivors)}")

    for j in survivors:
        j["is_new"] = j["id"] not in seen
    survivors.sort(key=lambda x: (not x["is_new"], -x["prefilter_score"]))
    tick("prefiltered", len(survivors), len(all_jobs))

    shortlist = survivors[:limit]
    if do_llm and shortlist:
        _, _, config = _ai_bits()
        if config.load_resume_text(config.load()).strip():
            llm_score(shortlist, verbose=False,
                      progress=lambda d, t: tick("scoring", d, t))
            if do_rank and len(shortlist) > 1:
                tick("ranking", 0, len(shortlist))
                shortlist = rank_candidates(shortlist, verbose=False)
        else:
            log("!! no resume text - skipping LLM stage")

    now = datetime.now(timezone.utc).isoformat()
    for j in all_jobs:
        seen.setdefault(j["id"], now)
    _save_state(state)

    # Drop the full JD - it is large and the UI does not need it.
    slim = [{k: v for k, v in j.items() if k != "description"}
            for j in shortlist]

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "sources": len(registry), "fetched": len(all_jobs),
            "rejected": rejected, "survivors": len(survivors),
            "shortlist": len(slim),
            "new": sum(1 for j in slim if j.get("is_new")),
        },
        "phases": per_phase,
        "jobs": slim,
    }
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PATH.write_text(json.dumps(payload, indent=2))
    tick("done", len(slim), len(slim))
    return payload


def load_last() -> dict:
    if LAST_PATH.exists():
        try:
            return json.loads(LAST_PATH.read_text())
        except ValueError:
            pass
    return {"generated": "", "counts": {}, "phases": [], "jobs": []}


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def report(jobs: list[dict], min_score: float, prefilter_only: bool) -> str:
    key = "prefilter_score" if prefilter_only else "llm_score"
    thresh = min_score * 10 if prefilter_only else min_score
    keep = [j for j in jobs if j.get(key, 0) >= thresh]
    keep.sort(key=lambda j: j.get(key, 0), reverse=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Job discovery - {now}", "",
             f"{len(jobs)} candidates survived prefilter, "
             f"{len(keep)} scored >= {thresh} on `{key}`.", ""]

    if not keep:
        lines += ["Nothing met the threshold. Lower it, add sources to "
                  "`~/.grapply/sources.json`, or widen the phases in "
                  "`~/.grapply/discovery_config.json`.", ""]

    if any(j.get("rank") for j in keep):
        keep.sort(key=lambda j: j.get("rank") or 999)
        lines += ["## Shortlist (ranked - your call)", "",
                  "| # | Role | Company | Location | Salary | Verdict | Why |",
                  "|---|------|---------|----------|--------|---------|-----|"]
        for j in keep:
            lines.append(
                f"| {j.get('rank','')} | {j['title']} | {j['company']} | "
                f"{j.get('location','')} | {j.get('salary','') or '-'} | "
                f"**{j.get('verdict','')}** | {j.get('why','')} |")
        lines.append("")

    for j in keep:
        flag = " (new)" if j.get("is_new") else ""
        lines.append(f"## {j['title']} - {j['company']}{flag}")
        lines.append("")
        lines.append(f"- **Location:** {j.get('location') or 'not stated'}")
        if j.get("salary"):
            lines.append(f"- **Salary:** {j['salary']}")
        lines.append(f"- **Source:** {j.get('source','')}"
                     + (f" (also on {', '.join(j['also_on'])})"
                        if j.get("also_on") else ""))
        lines.append(f"- **URL:** {j.get('url','')}")
        lines.append(f"- **Prefilter:** {j.get('prefilter_score')}/100")
        if not prefilter_only and "llm" in j:
            llm = j["llm"]
            lines.append(f"- **LLM fit:** {j.get('llm_score')}/10 - "
                         f"{llm.get('recommendation','?')}")
            for fld in ("matching_skills", "gaps"):
                v = llm.get(fld)
                if v:
                    if isinstance(v, list):
                        v = ", ".join(str(x) for x in v)
                    lines.append(f"- **{fld.replace('_',' ').title()}:** {v}")
        if j.get("hits"):
            lines.append("- **Matched:** " + "; ".join(
                f"{k}: {', '.join(v[:6])}" for k, v in j["hits"].items()))
        if j.get("penalties"):
            lines.append("- **Penalties:** " + ", ".join(j["penalties"]))
        for n in j.get("notes", []):
            lines.append(f"- **Note:** {n}")
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grapply job discovery")
    ap.add_argument("--min-score", type=float, default=7.5,
                    help="LLM fit threshold 0-10 (default 7.5)")
    ap.add_argument("--prefilter-min", type=float, default=None,
                    help="keyword score needed to reach the LLM stage")
    ap.add_argument("--prefilter-only", action="store_true",
                    help="no LLM - fast keyword triage only")
    ap.add_argument("--no-rank", action="store_true",
                    help="skip the comparative ranking pass")
    ap.add_argument("--rank", action="store_true",
                    help="(default) rank the shortlist comparatively")
    ap.add_argument("--limit", type=int, default=None,
                    help="max postings sent to the LLM stage")
    ap.add_argument("--phase", default="",
                    help="run only the phase whose name contains this")
    ap.add_argument("--validate", action="store_true",
                    help="check which registry sources respond, then exit")
    ap.add_argument("--discover", default="",
                    help="find a company's ATS board from its website")
    ap.add_argument("--add", action="store_true",
                    help="with --discover, append hits to sources.json")
    ap.add_argument("-o", "--out", default="",
                    help="write the report here (default: stdout)")
    args = ap.parse_args(argv)

    if args.discover:
        rows = src_mod.discover_ats(args.discover)
        if not rows:
            print("no ATS board found", file=sys.stderr)
            return 1
        for r in rows:
            ok, n = src_mod.validate_source(r)
            print(f"  {r['ats']:<16} {r['slug']:<24} {n:>4} jobs")
        if args.add:
            reg = load_sources()
            known = {(s.get("ats"), s.get("slug")) for s in reg}
            added = [r for r in rows if (r["ats"], r["slug"]) not in known]
            save_sources(reg + added)
            print(f"added {len(added)} source(s) to {SOURCES_PATH}",
                  file=sys.stderr)
        return 0

    if args.validate:
        for s in load_sources():
            ok, n = src_mod.validate_source(s)
            print(f"  {'ok' if ok else '--'} {s.get('ats',''):<16} "
                  f"{s.get('name', s.get('slug','')):<24} {n:>4}")
        return 0

    cfg = load_config()
    if args.phase:
        for p in cfg["phases"]:
            p["enabled"] = args.phase.lower() in p.get("name", "").lower()
        save_config(cfg)

    payload = run_scan(prefilter_min=args.prefilter_min, limit=args.limit,
                       do_llm=not args.prefilter_only,
                       do_rank=not args.no_rank)

    c = payload["counts"]
    print(f"\nfetched {c['fetched']}, rejected {c['rejected']}, "
          f"survivors {c['survivors']}, shortlist {c['shortlist']} "
          f"({c['new']} new)", file=sys.stderr)

    text = report(payload["jobs"], args.min_score, args.prefilter_only)
    if args.out:
        p = Path(args.out).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        print(f"wrote {p}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
