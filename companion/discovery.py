"""
grapply — job discovery

The missing half of grapply: instead of you finding a posting and grabbing it,
this goes and finds postings worth grabbing.

Design: poll the *ATS APIs* of a curated company registry, not aggregator sites.
Greenhouse, Lever, Ashby and SmartRecruiters all expose public unauthenticated
JSON with full job descriptions. No scraping, no Cloudflare, no captcha, and the
data is structured rather than guessed at.

Two-stage scoring, because LLM-scoring every posting is wasteful - Rocket Lab
alone publishes ~370 roles:

  stage 1  cheap local keyword prefilter (no LLM, milliseconds)
  stage 2  analyzer.score_job_fit() on survivors only (real LLM fit score 0-10)

Seen postings are remembered, so repeat runs only surface what is new.

  python -m companion.discovery --validate          # check the registry
  python -m companion.discovery --prefilter-only    # no LLM, fast triage
  python -m companion.discovery --min-score 7.5     # full run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable

STATE_PATH   = Path("~/.grapply/discovery_state.json").expanduser()
LAST_PATH    = Path("~/.grapply/discovery_last.json").expanduser()
SOURCES_PATH = Path("~/.grapply/sources.json").expanduser()
UA = "Mozilla/5.0 (X11; Linux x86_64) grapply-discovery/1.0"

# ── candidate profile: hard gates and weighted signals ───────────────────────
# Tune these. Every result reports which terms matched, so the threshold can be
# calibrated from real runs instead of guessed.

TITLE_REJECT = (
    "intern", "internship", "graduate", "new grad", "junior", "trainee",
    "apprentice", "student", "placement", "co-op", "co op",
)

REQUIRE_ANY = ("c++", "cpp", "c/c++")

# The JD body mentioning C++ is not enough - quant firms mention it in trader and
# analyst postings too. The TITLE has to describe an engineering role.
TITLE_REQUIRE_ANY = (
    "engineer", "developer", "programmer", "software", "architect",
    "sre", "devops", "lead", "leader",
)

# Some postings carry a senior-sounding title but say "Junior ..." in the first
# line. Eqvilent's "Software Developer (Algorithmic Engineering)" opens with
# "We are seeking a Junior Quantitative Developer". Check the opening text too.
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

LOCATION_OK = (
    "remote", "anywhere", "worldwide", "global", "distributed", "emea",
    "new zealand", "auckland", "wellington", "christchurch",
    "australia", "sydney", "melbourne", "brisbane", "perth", "apac",
)

HARD_BLOCK = (
    "security clearance", "ts/sci", "must be a u.s. citizen",
    "must be a us citizen", "us citizenship is required",
    "citizens only", "itar", "polygraph",
)

# US aerospace and defence firms paste ITAR / US-citizenship boilerplate into
# EVERY posting, including roles based in Auckland and staffed by locals. So the
# hard block only bites when the role is actually US-located; elsewhere it is
# downgraded to a note for you to verify.
NON_US_LOCATION = (
    "new zealand", "auckland", "wellington", "christchurch", "nz",
    "australia", "sydney", "melbourne", "brisbane", "perth",
    "netherlands", "amsterdam", "germany", "berlin", "munich", "london",
    "united kingdom", "singapore", "hong kong", "japan", "canada", "india",
)

WEIGHTS: dict[str, tuple[int, tuple[str, ...]]] = {
    "core_cpp": (24, (
        "c++", "c++11", "c++14", "c++17", "c++20", "c++23", "modern c++",
        "stl", "template", "raii", "object-oriented", "object oriented",
    )),
    "systems": (24, (
        "embedded linux", "embedded", "device driver", "driver development",
        "kernel", "rtos", "qnx", "real-time", "real time", "bare metal",
        "multithread", "multi-threaded", "concurrency", "lock-free",
        "memory model", "performance", "optimisation", "optimization",
        "profiling", "latency", "throughput", "linux internals", "posix",
        "cross-compil", "buildroot", "sanitizer", "valgrind",
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

# He genuinely lacks these. A keyword match that dies in the first technical
# call is worse than no match at all.
PENALTIES: dict[str, int] = {
    "unreal engine": 12, "unity": 10, "game engine": 10, "gameplay": 8,
    "animation": 5, "shader": 4,
    "yocto": 5, "bitbake": 5, "freertos": 4, "zephyr": 3,
    "microcontroller": 5, "bare-metal mcu": 6,
    "nmea": 3, "iec 62304": 4, "fpga": 4, "verilog": 5, "vhdl": 5,
    "php": 6, "ruby": 6, "salesforce": 10, "sap abap": 0,
}

DEFAULT_SOURCES: list[dict[str, str]] = [
    # verified working 30 Jul 2026
    {"ats": "greenhouse", "slug": "rocketlab",     "name": "Rocket Lab"},
    {"ats": "greenhouse", "slug": "eqvilentjobs",  "name": "Eqvilent"},
    {"ats": "greenhouse", "slug": "imc",           "name": "IMC Trading"},
    {"ats": "greenhouse", "slug": "janestreet",    "name": "Jane Street"},
    {"ats": "greenhouse", "slug": "dawnaerospace", "name": "Dawn Aerospace"},
    # add more - run --validate after editing ~/.grapply/sources.json
]


# ── http ─────────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 20.0) -> Any | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError):
        return None


# Some boards post titles with Cyrillic homoglyphs ("С++" with Cyrillic Es),
# which silently breaks keyword matching. Fold them to ASCII first.
_HOMOGLYPHS = str.maketrans({
    "\u0410": "A", "\u0412": "B", "\u0421": "C", "\u0415": "E", "\u041d": "H",
    "\u041a": "K", "\u041c": "M", "\u041e": "O", "\u0420": "P", "\u0422": "T",
    "\u0425": "X", "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p",
    "\u0441": "c", "\u0445": "x", "\u0443": "y",
})


def _fold(text: str) -> str:
    return (text or "").translate(_HOMOGLYPHS)


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw,
                 flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>|</p>|</li>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"[ \t]{2,}", " ", unescape(txt)).strip()


# ── ATS adapters: each yields normalised dicts ───────────────────────────────

def _from_greenhouse(slug: str, name: str) -> list[dict]:
    data = _get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id":      f"gh:{slug}:{j.get('id')}",
            "company": name,
            "title":   (j.get("title") or "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url":     j.get("absolute_url", ""),
            "posted":  j.get("updated_at", "") or j.get("first_published", ""),
            "description": _strip_html(j.get("content", "")),
        })
    return out


def _from_lever(slug: str, name: str) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        cats = j.get("categories", {}) or {}
        out.append({
            "id":      f"lever:{slug}:{j.get('id')}",
            "company": name,
            "title":   (j.get("text") or "").strip(),
            "location": cats.get("location", "") or "",
            "url":     j.get("hostedUrl", ""),
            "posted":  str(j.get("createdAt", "")),
            "description": _strip_html(
                j.get("descriptionPlain") or j.get("description", "")),
        })
    return out


def _from_ashby(slug: str, name: str) -> list[dict]:
    data = _get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id":      f"ashby:{slug}:{j.get('id')}",
            "company": name,
            "title":   (j.get("title") or "").strip(),
            "location": j.get("location", "") or "",
            "url":     j.get("jobUrl", ""),
            "posted":  j.get("publishedAt", ""),
            "description": _strip_html(
                j.get("descriptionHtml") or j.get("descriptionPlain", "")),
        })
    return out


def _from_smartrecruiters(slug: str, name: str) -> list[dict]:
    data = _get_json(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("content", []):
        loc = j.get("location", {}) or {}
        out.append({
            "id":      f"sr:{slug}:{j.get('id')}",
            "company": name,
            "title":   (j.get("name") or "").strip(),
            "location": ", ".join(
                x for x in (loc.get("city"), loc.get("country")) if x),
            "url":     f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            "posted":  j.get("releasedDate", ""),
            "description": "",   # SmartRecruiters needs a second call per job
        })
    return out


ADAPTERS = {
    "greenhouse":      _from_greenhouse,
    "lever":           _from_lever,
    "ashby":           _from_ashby,
    "smartrecruiters": _from_smartrecruiters,
}


# ── stage 1: cheap local prefilter ───────────────────────────────────────────

def prefilter(job: dict) -> dict:
    """Score 0-100 on keywords alone. No LLM. Explainable."""
    title = _fold(job["title"]).lower()
    loc   = _fold(job.get("location") or "").lower()
    body  = _fold(job.get("description", "")).lower()
    blob  = f"{title}\n{loc}\n{body}"

    reject: list[str] = []
    notes: list[str] = []
    non_us = any(t in loc for t in NON_US_LOCATION)
    if any(t in title for t in TITLE_REJECT):
        reject.append("title looks junior/intern")
    if not any(t in title for t in TITLE_REQUIRE_ANY):
        reject.append(f"title is not an engineering role: {job['title']}")
    if any(t in title for t in TITLE_REJECT_ROLE):
        reject.append(f"non-engineering role type: {job['title']}")
    opening = body[:1200]
    for t in BODY_LEVEL_REJECT:
        if t in opening:
            reject.append(f"body advertises a junior/entry role ('{t}')")
            break
    if not any(t in blob for t in REQUIRE_ANY):
        reject.append("no C/C++ signal")
    if not any(t in loc for t in LOCATION_OK) and "remote" not in blob:
        reject.append(f"location unreachable: {job.get('location') or '?'}")
    for t in HARD_BLOCK:
        if t in blob:
            if non_us:
                notes.append(
                    f"'{t}' appears in the text, but the role is located in "
                    f"{job.get('location')} - likely US template boilerplate. Verify.")
            else:
                reject.append(f"hard block: {t}")
            break

    hits: dict[str, list[str]] = {}
    score = 0.0
    for bucket, (weight, terms) in WEIGHTS.items():
        matched = [t for t in terms if t in (title if bucket == "seniority" else blob)]
        if matched:
            hits[bucket] = matched
            # saturating: 1 hit gets 55% of the weight, 4+ gets all of it
            frac = min(1.0, 0.55 + 0.15 * (len(matched) - 1))
            score += weight * frac

    pen: list[str] = []
    for term, cost in PENALTIES.items():
        if cost and term in blob:
            score -= cost
            pen.append(f"{term} (-{cost})")

    return {
        "prefilter_score": round(max(0.0, min(100.0, score)), 1),
        "hits": hits,
        "penalties": pen,
        "notes": notes,
        "reject": reject,
    }


# ── state ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except ValueError:
            pass
    return {"seen": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


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


# ── fetch ────────────────────────────────────────────────────────────────────

def fetch_all(sources: Iterable[dict], verbose: bool = True) -> list[dict]:
    jobs: list[dict] = []
    for src in sources:
        fn = ADAPTERS.get(src.get("ats", ""))
        if not fn:
            if verbose:
                print(f"  ?  unknown ats: {src}", file=sys.stderr)
            continue
        got = fn(src["slug"], src.get("name", src["slug"]))
        if verbose:
            mark = "ok" if got else "--"
            print(f"  {mark} {src['ats']:<16} {src.get('name', src['slug']):<22} {len(got):>4}")
        jobs.extend(got)
        time.sleep(0.3)
    return jobs


# ── stage 2: LLM fit score, survivors only ───────────────────────────────────

def llm_score(jobs: list[dict], verbose: bool = True) -> list[dict]:
    try:
        from . import ai_client as ai_module, analyzer, config
    except ImportError:
        import ai_client as ai_module, analyzer, config  # type: ignore

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
        try:
            res = analyzer.score_job_fit(job.get("description", ""), resume, ai)
        except Exception as exc:                      # noqa: BLE001
            res = {"error": str(exc), "score": 0}
        job["llm"] = res
        job["llm_score"] = float(res.get("score", 0) or 0)
    return jobs


# ── comparative ranking ──────────────────────────────────────────────────────

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
    """Second LLM pass: rank the shortlist against each other, not in isolation.

    Scoring each posting alone compresses everything into 8-9. Ranking them
    comparatively forces real separation, which is what a human needs to decide
    where to spend a limited number of applications.
    """
    if len(jobs) < 2:
        return jobs
    try:
        from . import ai_client as ai_module, analyzer, config  # noqa: F401
    except ImportError:
        import ai_client as ai_module, config  # type: ignore

    cfg    = config.load()
    resume = config.load_resume_text(cfg)
    ai     = ai_module.AIClient(cfg["ai"])

    # Compact representation - full JDs would blow the context for no gain.
    rows = []
    for j in jobs:
        llm = j.get("llm", {}) or {}
        rows.append(
            f"id={j['id']}\n"
            f"  title={j['title']}\n"
            f"  company={j['company']}  location={j.get('location','')}\n"
            f"  matched={'; '.join(f'{k}:{",".join(v[:5])}' for k, v in (j.get('hits') or {}).items())}\n"
            f"  llm_skills={llm.get('matching_skills','')}\n"
            f"  llm_gaps={llm.get('gaps','')}"
        )
    user = (f"CANDIDATE RESUME:\n{resume[:6000]}\n\n"
            f"ROLES TO RANK - there are exactly {len(jobs)}. Your JSON array MUST "
            f"contain exactly {len(jobs)} objects, ranks 1..{len(jobs)}, every id "
            f"used once. Do not truncate the list.\n\n" + "\n\n".join(rows))

    if verbose:
        print(f"  ranking {len(jobs)} roles comparatively...", file=sys.stderr)
    try:
        raw = ai.generate(RANK_SYSTEM, user, _endpoint="analyze")
        m = re.search(r"\[.*\]", raw, re.S)
        order = json.loads(m.group(0)) if m else []
    except Exception as exc:                                  # noqa: BLE001
        print(f"  !! ranking failed: {exc}", file=sys.stderr)
        return jobs

    by_id = {j["id"]: j for j in jobs}
    for row in order:
        j = by_id.get(row.get("id", ""))
        if j:
            j["rank"]    = row.get("rank")
            j["verdict"] = row.get("verdict", "")
            j["why"]     = row.get("why", "")
    ranked   = sorted((j for j in jobs if j.get("rank")), key=lambda x: x["rank"])
    unranked = [j for j in jobs if not j.get("rank")]
    return ranked + unranked


# ── report ───────────────────────────────────────────────────────────────────

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
        lines += ["Nothing met the threshold. Lower it, or add sources to "
                  "`~/.grapply/sources.json`.", ""]

    if any(j.get("rank") for j in keep):
        keep.sort(key=lambda j: j.get("rank") or 999)
        lines += ["## Shortlist (ranked - your call)", "",
                  "| # | Role | Company | Location | Verdict | Why |",
                  "|---|------|---------|----------|---------|-----|"]
        for j in keep:
            lines.append(
                f"| {j.get('rank','')} | {j['title']} | {j['company']} | "
                f"{j.get('location','')} | **{j.get('verdict','')}** | "
                f"{j.get('why','')} |")
        lines.append("")

    for j in keep:
        lines.append(f"## {j['title']} - {j['company']}")
        lines.append("")
        lines.append(f"- **Location:** {j.get('location') or 'not stated'}")
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
        hits = j.get("hits", {})
        if hits:
            lines.append("- **Matched:** " + "; ".join(
                f"{k}: {', '.join(v[:6])}" for k, v in hits.items()))
        if j.get("penalties"):
            lines.append("- **Penalties:** " + ", ".join(j["penalties"]))
        for n in j.get("notes", []):
            lines.append(f"- **Note:** {n}")
        lines.append("")
    return "\n".join(lines)


# ── programmatic entry point (used by the companion HTTP API) ────────────────

def run_scan(prefilter_min: float = 55.0, limit: int = 25,
             do_llm: bool = True, do_rank: bool = True,
             include_seen: bool = False,
             progress: "callable | None" = None) -> dict:
    """Full scan, returning structured results and persisting them to disk.

    progress(stage: str, done: int, total: int) is called as work proceeds so a
    UI can show something during the ~2 minutes an LLM pass takes.
    """
    def tick(stage: str, done: int = 0, total: int = 0) -> None:
        if progress:
            try:
                progress(stage, done, total)
            except Exception:                              # noqa: BLE001
                pass

    sources = load_sources()
    tick("fetching", 0, len(sources))
    jobs = fetch_all(sources, verbose=False)
    tick("fetched", len(jobs), len(jobs))

    state = _load_state()
    seen  = state.setdefault("seen", {})

    survivors, rejected = [], 0
    for j in jobs:
        if not include_seen and j["id"] in seen:
            continue
        j.update(prefilter(j))
        if j["reject"]:
            rejected += 1
            continue
        if j["prefilter_score"] >= prefilter_min:
            survivors.append(j)
    survivors.sort(key=lambda x: x["prefilter_score"], reverse=True)
    tick("prefiltered", len(survivors), len(jobs))

    survivors = survivors[:limit]
    if do_llm and survivors:
        try:
            from . import ai_client as ai_module, analyzer, config
        except ImportError:
            import ai_client as ai_module, analyzer, config      # type: ignore
        cfg    = config.load()
        resume = config.load_resume_text(cfg)
        if resume.strip():
            ai = ai_module.AIClient(cfg["ai"])
            for i, job in enumerate(survivors, 1):
                tick("scoring", i, len(survivors))
                try:
                    res = analyzer.score_job_fit(
                        job.get("description", ""), resume, ai)
                except Exception as exc:                     # noqa: BLE001
                    res = {"error": str(exc), "score": 0}
                job["llm"] = res
                job["llm_score"] = float(res.get("score", 0) or 0)
            if do_rank and len(survivors) > 1:
                tick("ranking", 0, len(survivors))
                survivors = rank_candidates(survivors, verbose=False)

    for j in jobs:
        seen.setdefault(j["id"], datetime.now(timezone.utc).isoformat())
    _save_state(state)

    # Drop the full JD text - it is large and the UI does not need it.
    slim = []
    for j in survivors:
        slim.append({k: v for k, v in j.items() if k != "description"})

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {"sources": len(sources), "fetched": len(jobs),
                   "rejected": rejected, "shortlist": len(slim)},
        "jobs": slim,
    }
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PATH.write_text(json.dumps(payload, indent=2))
    tick("done", len(slim), len(slim))
    return payload


def load_last() -> dict:
    """Most recent scan results, or an empty shell if none have run."""
    if LAST_PATH.exists():
        try:
            return json.loads(LAST_PATH.read_text())
        except ValueError:
            pass
    return {"generated": "", "counts": {}, "jobs": []}


# ── cli ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grapply job discovery")
    ap.add_argument("--min-score", type=float, default=7.5,
                    help="LLM fit threshold 0-10 (default 7.5 = your 75%%)")
    ap.add_argument("--prefilter-min", type=float, default=45.0,
                    help="keyword score needed to reach the LLM stage")
    ap.add_argument("--prefilter-only", action="store_true",
                    help="no LLM - fast keyword triage only")
    ap.add_argument("--all", action="store_true",
                    help="include postings already seen")
    ap.add_argument("--validate", action="store_true",
                    help="check which sources respond, then exit")
    ap.add_argument("--rank", action="store_true",
                    help="second LLM pass: rank the shortlist comparatively")
    ap.add_argument("--limit", type=int, default=25,
                    help="max postings sent to the LLM stage")
    ap.add_argument("-o", "--out", default="",
                    help="write the report here (default: stdout)")
    args = ap.parse_args(argv)

    sources = load_sources()
    print(f"sources: {len(sources)} (edit {SOURCES_PATH})", file=sys.stderr)

    if args.validate:
        fetch_all(sources)
        return 0

    jobs = fetch_all(sources)
    print(f"fetched {len(jobs)} postings", file=sys.stderr)

    state = _load_state()
    seen  = state.setdefault("seen", {})

    survivors = []
    for j in jobs:
        if not args.all and j["id"] in seen:
            continue
        j.update(prefilter(j))
        if j["reject"]:
            continue
        if j["prefilter_score"] >= args.prefilter_min:
            survivors.append(j)

    survivors.sort(key=lambda x: x["prefilter_score"], reverse=True)
    print(f"{len(survivors)} passed prefilter (>= {args.prefilter_min})",
          file=sys.stderr)

    if not args.prefilter_only and survivors:
        survivors = llm_score(survivors[: args.limit])
        if args.rank:
            survivors = rank_candidates(survivors)

    text = report(survivors, args.min_score, args.prefilter_only)

    for j in jobs:
        seen.setdefault(j["id"], datetime.now(timezone.utc).isoformat())
    _save_state(state)

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
