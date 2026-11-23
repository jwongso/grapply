"""
grapply - job discovery

Finds postings worth applying to, instead of waiting for you to find them.

Search runs in phases, in priority order. The default is the shape most job
hunts actually have: everything local first, then remote-worldwide as the
fallback. Each phase decides its own sources and its own location gate, so
"remote" means remote in the phase that wants it and is irrelevant in the one
that does not.

  phase 1  German cities Arbeitnow + LinkedIn + Indeed + company registry
  phase 2  Remote        Germany-reachable remote aggregators + the registry

Each phase names a market (discovery.MARKETS) that supplies its location gate
and its default sources. Markets are data, so scanning a different country is a
config edit or a one-off `--market germany` - not a code change.

Scoring stays two-stage, because running an LLM over every posting is wasteful
when Seek alone returns hundreds:

  stage 1  keyword prefilter, local, milliseconds, fully explainable
  stage 2  LLM fit score, then a comparative ranking pass, survivors only

Seen postings are remembered but NOT hidden. Hiding them was why a rescan kept
showing an empty or week-old list: everything had been seen once, so nothing
survived. A posting now disappears only when you dismiss or apply to it.

  python -m companion.discovery --validate       # check the registry
  python -m companion.discovery --list-markets   # show the known markets
  python -m companion.discovery --market germany # one-off scan of a market
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
from urllib.parse import urlsplit

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
    # German and Dutch boards post these in their own language.
    "praktikum*", "praktikant*", "werkstudent*", "ausbildung*", "azubi*",
    "berufseinsteiger*", "duales studium", "stage", "stagiaire",
)

# The NZ market is far too small to gate on C++ alone - that rejected roughly
# 97% of everything fetched. C#/.NET (Framecad, AWS-backed services) and Python
# (SAP tooling, current work) are both real, shipped experience, so a posting
# qualifies on any of the three. Stack-agnostic software roles can also pass;
# priority order is reflected in the weights rather than a C++-only gate.
# Defence industry. A scan for "senior C++ engineer" in Berlin is mostly
# drone, counter-drone and weapons start-ups, so they are excluded by default.
# Company names are matched as whole words against the posting's company. The
# text terms are phrases, not the bare word "military", because US equal
# opportunity boilerplate ("protected veteran or military status") would
# otherwise reject half the postings. Add your own with `exclude_companies` /
# `exclude_terms` in the discovery config, or set `allow_defence: true` to turn
# the whole filter off.
DEFENCE_COMPANIES = (
    "helsing", "stark", "quantum systems", "tytan", "hensoldt", "rheinmetall",
    "mbda", "diehl defence", "diehl defense", "krauss-maffei wegmann", "kmw",
    "atlas elektronik", "hypersonica", "alpine eagle", "arx robotics",
    "donaustahl", "anduril", "shield ai", "saab", "bae systems", "thales",
    "leonardo", "airbus defence", "airbus defense", "terma", "nordic air defence",
    "cambridge aerospace", "destinus", "tekever", "skyeton",
)
DEFENCE_TERMS = (
    "defence industry", "defense industry", "defence sector", "defense sector",
    "defence technology", "defense technology", "defence customers",
    "defense customers", "defence and security", "defense and security",
    "weapon*", "munition*", "bundeswehr", "warfighter*", "armed forces",
    "dual-use", "dual use", "counter-drone", "counter-uas", "loitering",
)


# AI training platforms. These list "Software Engineer" and "Python Engineer"
# roles that are really freelance work ranking or writing code for model
# training, not engineering jobs. `allow_ai_training: true` turns this off.
AI_TRAINING_COMPANIES = (
    "alignerr", "dataannotation", "data annotation", "outlier", "scale ai",
    "remotasks", "mercor", "micro1", "surge ai", "toloka", "appen",
    "invisible technologies", "crowdgen", "telus digital", "prolific",
    "handshake ai",
)
AI_TRAINING_TERMS = (
    "ai trainer*", "ai training platform", "data annotation", "annotator*",
    "data labeling", "data labelling", "ai tutor*",
)


def exclusion_reason(job: dict, cfg: dict | None = None) -> str:
    """Why a posting is excluded by industry, or "" when it is not."""
    cfg = cfg or {}
    companies: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    if not cfg.get("allow_defence"):
        companies, terms = DEFENCE_COMPANIES, DEFENCE_TERMS
    if not cfg.get("allow_ai_training"):
        companies += AI_TRAINING_COMPANIES
        terms += AI_TRAINING_TERMS
    companies = tuple(companies) + tuple(cfg.get("exclude_companies") or ())
    terms = tuple(terms) + tuple(cfg.get("exclude_terms") or ())
    company = src_mod.fold(job.get("company") or "").lower()
    hit = next((c for c in companies if c and _has(company, c.lower())), "")
    if hit:
        return f"excluded industry: {job.get('company')} ({hit})"
    text = src_mod.fold(
        f"{job.get('title') or ''}\n{(job.get('description') or '')[:2500]}"
    ).lower()
    hit = next((t for t in terms if t and _has(text, t.lower())), "")
    if hit:
        return f"excluded industry: text mentions '{hit.rstrip('*')}'"
    return ""


REQUIRE_ANY = ("c++", "cpp", "c/c++", "c#", "csharp", ".net", "dotnet",
               "python")

# A JD body mentioning C++ is not enough - quant firms name it in trader and
# analyst postings too. The title has to describe an engineering role.
GERMAN_DEV_TITLES = (
    "entwickler*", "softwareentwickler*", "softwareingenieur*",
    "softwarearchitekt*", "informatiker*", "fachinformatiker*",
    "programmierer*", "webentwickler*", "anwendungsentwickler*",
    "applikationsentwickler*", "firmwareentwickler*", "backendentwickler*",
    "frontendentwickler*", "fullstackentwickler*", "embeddedentwickler*",
)

TITLE_REQUIRE_ANY = (
    "engineer", "developer", "programmer", "software", "architect",
    "sre", "devops", "lead", "leader",
) + GERMAN_DEV_TITLES

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

# Engineering disciplines that are not software engineering. TITLE_REQUIRE_ANY
# accepts "engineer" and "lead" on their own, which at an aerospace employer
# matches the entire mechanical and RF organisation - a scan of Rocket Lab
# surfaced Team Lead CFD, Thermofluids Analysis, Senior Vibrations Engineer and
# RF Ground Segment, none of which are software roles. Their job descriptions
# legitimately mention Python and Linux, so only the title distinguishes them.
#
# FPGA and ASIC are here rather than in PENALTIES because a title-level match
# means the role IS hardware description work, not a role that merely touches
# it - that is a stated gap, not a discount.
SOFTWARE_TITLE_TERMS = (
    "software", "developer", "programmer", "firmware", "embedded",
    "backend", "back-end", "frontend", "front-end", "full stack",
    "full-stack", "devops", "sre", "platform", "application", "web",
    "cloud", "data engineer", "machine learning", "ml engineer",
) + GERMAN_DEV_TITLES

# Trailing "*" marks a prefix, which most of these need - titles pluralise
# ("Thermofluids Analysis", "Senior Vibrations Engineer") and a whole-word
# match silently misses them.
TITLE_REJECT_DISCIPLINE = (
    "cfd", "thermofluid*", "thermal", "vibration*", "dynamics", "structural",
    "mechanical", "propulsion", "aerodynamic*", "aerothermal", "stress",
    "fatigue", "material*", "metallurg*", "manufacturing", "machinist",
    "welding", "composite*", "tooling", "fixture",
    "qualification", "hardware test", "test technician",
    "rf ", "radio frequency", "antenna*", "microwave", "photonic*", "optical",
    "electrical", "electronic*", "chemical", "civil", "mechatronic*",
    "avionics hardware",
)

# Hardware description work. Kept separate because these must NOT be rescued by
# a software word in the title: "FPGA Developer" is exactly the role being
# excluded, and Verilog/VHDL are a stated gap rather than a discount.
TITLE_REJECT_HARDWARE = ("fpga", "asic", "verilog", "vhdl", "pcb", "rtl design")

HARD_BLOCK = (
    "must be a u.s. citizen",
    "must be a us citizen", "us citizenship is required",
    "citizens only", "itar",
)

# Any mention of a security clearance rejects the role, wherever it is located.
# HARD_BLOCK softens to a note outside the US because American aerospace firms
# paste ITAR boilerplate into every posting, but a clearance statement is a real
# requirement or a sign of defence work, and the answer here is always no. The
# German equivalents are included, with and without umlauts.
CLEARANCE_BLOCK = (
    "security clearance", "security-clearance", "ts/sci", "polygraph",
    "clearance required", "eligible for clearance", "sc clearance",
    "sicherheitsüberprüfung*", "sicherheitsueberpruefung*",
    "sicherheitsuberprufung*", "sicherheitsfreigabe*", "sicherheitsbescheid*",
    "geheimschutz*", "verschlusssache*", "ü2-überprüfung", "ue2-ueberpruefung",
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

# "Remote" on a remote job board almost never means "remote from anywhere".
# Measured across 512 aggregator postings: only 8 said Worldwide and 6 Global,
# while 73 were US-only, 16 EMEA, and many named a single city. Every one of
# them still arrived with remote=True, so the flag alone is worthless as a gate.

# Generic words that describe the arrangement but name no place. A location
# made only of these is unrestricted as far as we can tell.
REMOTE_GENERIC = ("remote", "fully remote", "remote-first", "remote only",
                  "distributed", "work from home", "wfh", "homeoffice",
                  "home office", "telecommute", "anywhere", "flexible")

# Places a New Zealand resident can actually work from. Australia counts: he
# has no AU work rights, but contracting remotely from NZ into an AU company
# is the arrangement job_target_list.md already plans for.
NZ_REACHABLE_REGIONS = (
    "worldwide", "global", "anywhere", "international",
    "new zealand", "nz", "aotearoa", "auckland", "wellington", "christchurch",
    "apac", "asia pacific", "asia-pacific", "oceania", "australia", "sydney",
    "melbourne", "brisbane", "perth",
)

# Phrases in the body that gate on work authorisation he does not hold. These
# are stronger evidence than the location string, which is often just a
# head-office address.
# A board serves a market, and a posting with a blank or generic location
# inherits it. Arbeitnow is a German board: its "Homeoffice" and blank-location
# postings are Germany-based roles wanting EU work rights, and they sailed
# through the location gate until this was added. Language markers in the title
# ("(m/w/d)", "(m/f/d)") corroborate it.
#
# Germany is now the default market, so these postings pass the gate; the
# default region only rejects them for other markets (e.g. a NZ or UK scan).
SOURCE_DEFAULT_REGION = {
    "arbeitnow": "Germany",
}

WORK_AUTH_BLOCK = (
    "must be authorized to work in the us",
    "must be authorised to work in the us",
    "must be legally authorized to work in the united states",
    "us work authorization required",
    "eligible to work in the us",
    "eligible to work in the uk",
    "must reside in the united states",
    "must be based in the us",
    "must be located in the united states",
    "right to work in the uk",
    "eu work permit",
)

# ══════════════════════════════════════════════════════════════════════════════
# Markets - the geography half of a scan, made data instead of code
# ══════════════════════════════════════════════════════════════════════════════
# A phase used to carry a hard-coded gate string ("nz" / "remote") and the gate
# logic branched on it against module-level term tuples. That made "scan Germany
# instead" a code change. A market is the same information as data:
#
#   in_market         office locations that put a posting in this market
#   reachable_remote  region tokens a person based here can work remotely for
#   work_auth_block   JD phrases that gate on work rights a person here lacks
#   require_remote    reject anything that is not remote (worldwide-style phase)
#   seek_sites / aggregators / browser_profiles / where
#                     the sources that make sense for this market, used when a
#                     phase does not name its own
#
# Everything else about a scan (candidate profile, scoring, dedupe) is
# geography-independent. Add a market here and it is immediately selectable from
# the CLI (--market) and the discovery page. A phase picks one with
# `"market": "<name>"`; the legacy `"gate"` key is still honoured as an alias.

DEFAULT_KEYWORDS: list[str] = [
    "senior software engineer", "senior software developer",
    "staff software engineer", "principal software engineer",
    "lead software engineer", "senior AI engineer", "c++", "embedded software", "firmware", "softwareentwickler",
]

# A JD that plainly says work-from-anywhere clears the gate for every market,
# even when it also prints a head-office city.
# The bare word "worldwide" is deliberately not here: "customers worldwide" and
# "offices worldwide" describe the company, not where the role can be done.
_REMOTE_ANYWHERE = ("remote worldwide", "worldwide remote", "work worldwide",
                    "anywhere in the world", "from anywhere",
                    "work from anywhere", "globally remote",
                    "fully distributed")

# Body-only evidence that a role is remote. "Some flexibility for remote work"
# is a hybrid employer, so the bare word "remote" is not enough when the board
# gave no remote flag.
_REMOTE_STRONG = (
    "fully remote", "100% remote", "full remote", "fully-remote",
    "remote-first", "remote first", "remote only", "remote position",
    "remote role", "remote job", "fully distributed", "work from anywhere",
    "completely remote", "entirely remote", "remote within germany",
    "remote in germany", "remote from germany", "germany remote",
    "vollstandig remote", "komplett remote", "voll remote",
    "100% homeoffice", "100% home office", "100 % remote",
)

# A role that asks for office days is not remote, whatever else it says.
_ONSITE_HINT = (
    "hybrid", "on-site", "onsite", "on site", "in the office", "in our office",
    "days a week in", "days per week in", "vor ort", "prasenz", "bureau",
)

# Words that name a country or region, not an office. A location made only of
# these, plus arrangement words, still names no place to commute to.
_COUNTRY_LEVEL = ("germany", "deutschland", "europe", "european union", "eu",
                  "emea", "eea", "dach", "worldwide", "global",
                  "international", "united states", "usa", "uk")

_US_AUTH_BLOCK = (
    "must be authorized to work in the us",
    "must be authorised to work in the us",
    "must be legally authorized to work in the united states",
    "us work authorization required",
    "eligible to work in the us",
    "must reside in the united states",
    "must be based in the us",
    "must be located in the united states",
)


def _mk(label: str, *, where: str = "",
        seek_sites: tuple[str, ...] = (),
        browser_profiles: tuple[str, ...] = (),
        aggregators: tuple[str, ...] = ("arbeitnow", "remoteok", "remotive",
                                        "jobicy", "workingnomads", "himalayas"),
        in_market: tuple[str, ...] = (),
        reachable_remote: tuple[str, ...] = (),
        work_auth_block: tuple[str, ...] = (),
        require_remote: bool = False,
        linkedin: bool = False) -> dict[str, Any]:
    return {
        "label": label,
        "where": where,
        "linkedin": linkedin,
        "seek_sites": list(seek_sites),
        "browser_profiles": list(browser_profiles),
        "aggregators": list(aggregators),
        "in_market": tuple(t.lower() for t in in_market),
        "reachable_remote": tuple(
            dict.fromkeys(t.lower() for t in
                          (("worldwide", "global", "anywhere", "international")
                           + tuple(reachable_remote)))),
        "work_auth_block": tuple(t.lower() for t in work_auth_block),
        "require_remote": require_remote,
    }


GERMANY_TERMS = (
    "germany", "deutschland", "berlin", "munich", "munchen", "muenchen",
    "hamburg", "frankfurt", "cologne", "koln", "koeln", "stuttgart",
    "dusseldorf", "duesseldorf", "leipzig", "dortmund", "dresden", "hannover",
    "hanover", "nuremberg", "nurnberg", "bremen", "karlsruhe", "mannheim",
    "dach")

# Places a person living in Germany can work remotely for. German city names
# are included because boards often tag a remote role with its office city.
GERMANY_REACHABLE = (
    "germany", "deutschland", "europe", "european union", "eu", "emea", "eea",
    "cet", "cest", "central european", "dach", "remote europe",
    "europe-based", "europe based") + GERMANY_TERMS

APPROVED_CITIES = ("berlin", "hamburg", "frankfurt", "munich", "münchen", "munchen", "muenchen")

MARKETS: dict[str, dict[str, Any]] = {
    "nz": _mk(
        "New Zealand", where="New Zealand", seek_sites=("nz",),
        browser_profiles=("indeed-nz",),
        in_market=NZ_TERMS,
        reachable_remote=NZ_REACHABLE_REGIONS,
        work_auth_block=_US_AUTH_BLOCK + (
            "eligible to work in the uk", "right to work in the uk",
            "eu work permit", "must be based in the eu",
            "eligible to work in the eu"),
    ),
    "au": _mk(
        "Australia", where="Australia", seek_sites=("au",),
        in_market=("australia", "sydney", "melbourne", "brisbane", "perth",
                   "canberra", "adelaide", "hobart", "gold coast", "au"),
        reachable_remote=("australia", "new zealand", "apac", "asia pacific",
                          "asia-pacific", "oceania", "anz"),
        work_auth_block=_US_AUTH_BLOCK + (
            "right to work in the uk", "eu work permit"),
    ),
    "germany": _mk(
        "Germany", where="Germany",
        browser_profiles=("indeed-de",),
        aggregators=("arbeitnow", "remoteok", "remotive", "himalayas",
                     "workingnomads"),
        in_market=GERMANY_TERMS,
        reachable_remote=GERMANY_REACHABLE,
        work_auth_block=_US_AUTH_BLOCK + (
            "right to work in the uk", "eligible to work in the uk",
            "must be a u.s. citizen", "must be a us citizen"),
    ),
    # The two default phases: on-site in Berlin, then remote from Germany.
    "berlin": _mk(
        "Berlin, Germany", where="Berlin, Germany", linkedin=True,
        browser_profiles=("indeed-de",),
        aggregators=("arbeitnow",),
        in_market=("berlin",),
        reachable_remote=GERMANY_REACHABLE,
        work_auth_block=_US_AUTH_BLOCK + (
            "right to work in the uk", "eligible to work in the uk"),
    ),
    "germany-remote": _mk(
        "Germany (remote)", where="Germany",
        aggregators=("arbeitnow", "remotive", "himalayas", "workingnomads",
                     "remoteok", "jobicy"),
        reachable_remote=GERMANY_REACHABLE,
        work_auth_block=_US_AUTH_BLOCK + (
            "right to work in the uk", "eligible to work in the uk"),
        require_remote=True,
    ),
    "uk": _mk(
        "United Kingdom",
        aggregators=("arbeitnow", "remoteok", "remotive", "himalayas",
                     "workingnomads"),
        in_market=("united kingdom", "uk", "england", "scotland", "wales",
                   "london", "manchester", "birmingham", "edinburgh",
                   "glasgow", "bristol", "leeds", "cambridge", "oxford"),
        reachable_remote=("united kingdom", "uk", "europe", "emea", "eu",
                          "gmt", "bst", "gb"),
        work_auth_block=_US_AUTH_BLOCK,
    ),
    "eu-remote": _mk(
        "Europe (remote)",
        in_market=(),
        reachable_remote=("europe", "european union", "eu", "emea", "eea",
                          "cet", "cest", "central european", "dach", "gmt",
                          "germany", "netherlands", "spain", "poland",
                          "portugal", "france", "remote europe"),
        work_auth_block=_US_AUTH_BLOCK,
        require_remote=True,
    ),
    "remote": _mk(
        "Remote worldwide",
        in_market=(),
        reachable_remote=GERMANY_REACHABLE,
        work_auth_block=WORK_AUTH_BLOCK,
        require_remote=True,
    ),
}

# Independent office destinations, not a claim that Hamburg is commutable
# from Berlin. Keep the Berlin-only market available for narrower scans.
MARKETS["german-cities"] = _mk(
    "Berlin preferred; Hamburg, Frankfurt, Munich", where="Germany", linkedin=True,
    browser_profiles=("indeed-de",), aggregators=("arbeitnow",),
    in_market=APPROVED_CITIES, reachable_remote=GERMANY_REACHABLE,
    work_auth_block=MARKETS["berlin"]["work_auth_block"],
)
for _city, _aliases in (("hamburg", ("hamburg",)), ("frankfurt", ("frankfurt",)),
                         ("munich", ("munich", "münchen", "munchen", "muenchen"))):
    MARKETS[_city] = _mk(
        f"{_city.title()}, Germany", where=f"{_city.title()}, Germany", linkedin=True,
        browser_profiles=("indeed-de",), aggregators=("arbeitnow",),
        in_market=_aliases, reachable_remote=GERMANY_REACHABLE,
        work_auth_block=MARKETS["berlin"]["work_auth_block"],
    )

for _market_name in ("germany", "berlin", "german-cities", "hamburg", "frankfurt",
                     "munich", "germany-remote", "eu-remote", "remote"):
    MARKETS[_market_name]["require_remote_region_evidence"] = True


def resolve_market(spec: Any, cfg: dict | None = None) -> dict | None:
    """Turn a phase dict, a market-name string, or None into a market dict.

    Returns None to mean "no location gate at all". Raises ValueError for a
    name that is neither a built-in market nor defined in cfg["markets"], so a
    typo in --market fails loudly instead of silently scanning the wrong place.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        name = spec.strip().lower()
    elif isinstance(spec, dict):
        name = str(spec.get("market") or spec.get("gate") or "").strip().lower()
    else:
        return None
    if not name:
        return None

    overrides = (cfg or {}).get("markets") or {}
    if isinstance(overrides.get(name), dict):
        base = dict(MARKETS.get(name) or _mk(name.replace("-", " ").title()))
        base.update(overrides[name])
        base.setdefault("label", name.replace("-", " ").title())
        for key in ("in_market", "reachable_remote", "work_auth_block"):
            base[key] = tuple(str(t).lower() for t in base.get(key, ()))
        return base
    if name in MARKETS:
        return MARKETS[name]
    raise ValueError(f"unknown market {name!r}; "
                     f"known: {', '.join(sorted(MARKETS))}")


def _synthetic_phase(name: str, market: dict, cfg: dict) -> dict:
    """A one-off phase for `--market X` / `?market=X` - scan that market now,
    without touching the saved phase configuration."""
    return {
        "name": market["label"],
        "enabled": True,
        "market": name,
        "keywords": list(cfg.get("default_keywords") or DEFAULT_KEYWORDS),
        "seek_sites": list(market.get("seek_sites", [])),
        "aggregators": list(market.get("aggregators", [])),
        "linkedin": bool(market.get("linkedin")),
        "browser_profiles": list(market.get("browser_profiles", [])),
        "browser_pages": 6,
        "browser_keywords": 1,
        "use_registry": True,
        "max_jobs": 400,
    }


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
    "software_delivery": (20, (
        "backend", "api design", "architecture", "testing", "code review",
        "reliability", "distributed systems", "mentoring", "observability",
    )),
    "applied_ai": (20, (
        "llm", "rag", "retrieval", "agents", "agentic", "evaluation",
        "inference", "embeddings", "tool calling", "mcp", "generative ai",
        "ai engineer", "artificial intelligence", "machine learning",
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
    {"ats": "greenhouse", "slug": "imc",           "name": "IMC Trading"},
    {"ats": "greenhouse", "slug": "janestreet",    "name": "Jane Street"},
    # Germany-based employers (slugs verified live 2026-11-01).
    {"ats": "greenhouse", "slug": "celonis", "name": "Celonis"},
    {"ats": "greenhouse", "slug": "n26", "name": "N26"},
    {"ats": "greenhouse", "slug": "getyourguide", "name": "GetYourGuide"},
    {"ats": "greenhouse", "slug": "contentful", "name": "Contentful"},
    {"ats": "greenhouse", "slug": "grover", "name": "Grover"},
    {"ats": "greenhouse", "slug": "sumup", "name": "SumUp"},
    {"ats": "greenhouse", "slug": "flix", "name": "FlixBus"},
    {"ats": "greenhouse", "slug": "raisin", "name": "Raisin"},
    {"ats": "greenhouse", "slug": "helsing", "name": "Helsing"},
    {"ats": "greenhouse", "slug": "isaraerospace", "name": "Isar Aerospace"},
    {"ats": "greenhouse", "slug": "trivago", "name": "trivago"},
    {"ats": "ashby", "slug": "deepl", "name": "DeepL"},
    {"ats": "ashby", "slug": "n8n", "name": "n8n"},
    {"ats": "ashby", "slug": "parloa", "name": "Parloa"},
    {"ats": "ashby", "slug": "langdock", "name": "Langdock"},
    {"ats": "ashby", "slug": "black-forest-labs", "name": "Black Forest Labs"},
    {"ats": "ashby", "slug": "tacto", "name": "Tacto"},
    {"ats": "smartrecruiters", "slug": "BoschGroup", "name": "Bosch"},
]

# Seek is queried per keyword, so keep the list short and high-signal - each
# term is a full paginated search.
DEFAULT_CONFIG: dict[str, Any] = {
    "phases": [
        {
            "name": "Berlin preferred; Hamburg, Frankfurt, Munich",
            "enabled": True,
            "market": "german-cities",
            # Searching "c++" alone is too narrow: boards tokenise the plus
            # signs unpredictably and plenty of matching roles never put it in
            # the title. Cast wide on job titles and let the prefilter evaluate
            # language evidence or a genuinely stack-agnostic software role.
            "keywords": ["senior software engineer", "senior software developer",
                         "staff software engineer", "principal software engineer",
                         "lead software engineer", "senior AI engineer",
                         "c++", "embedded software", "firmware",
                         "softwareentwickler"],
            # LinkedIn's public guest search, no login. Berlin has far more
            # roles there than on Arbeitnow or in the company registry.
            "linkedin": True,
            # Seek does not operate in Germany; Arbeitnow is the German board.
            "seek_sites": [],
            "aggregators": ["arbeitnow"],
            # Indeed needs a headed, signed-in browser (page 2+ is gated behind
            # login), which the render layer handles. Seek is deliberately not
            # here: it server-renders and its JSON API returns more, faster.
            "browser_profiles": ["indeed-de"],
            "browser_pages": 6,
            "use_registry": True,
            "max_jobs": 400,
        },
        {
            "name": "Germany (remote)",
            "enabled": True,
            "market": "germany-remote",
            "keywords": ["c++", "senior software engineer",
                         "staff software engineer", "principal software engineer",
                         "lead software engineer"],
            "seek_sites": [],
            "aggregators": ["arbeitnow", "remoteok", "remotive", "jobicy",
                            "workingnomads", "himalayas"],
            "browser_profiles": [],
            "use_registry": True,
            "max_jobs": 400,
        },
    ],
    # Per-market overrides, keyed by market name. A dict here is merged over the
    # built-in MARKETS entry (or creates a new market if the name is unknown),
    # so a scan's geography can be tuned without editing code.
    "markets": {},
    # Keywords for an on-demand `--market X` scan, which has no phase of its own.
    "default_keywords": DEFAULT_KEYWORDS,
    "prefilter_min": 45.0,
    "llm_limit": 25,
    # New installations require an original employer publication within seven
    # days. Explicit saved settings are preserved; age=0 disables only the age
    # limit, not the independent requirement for original-date evidence.
    "max_age_days": 7,
    "require_verified_posted": True,
    "allow_stack_agnostic": True,
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

def parse_posted(value: Any) -> datetime | None:
    """Best-effort parse of a source's `posted` field to an aware UTC datetime.

    Every source spells this differently: Seek and the ATS boards emit ISO
    8601, the RSS-backed aggregators emit RFC 2822 pubDates, and a couple hand
    back epoch seconds or milliseconds. Returns None when it cannot tell, which
    the caller must treat as unknown rather than old.
    """
    if value is None or value == "":
        return None

    # Epochs arrive as both ints and digit strings, in seconds and in ms.
    if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        if n <= 0:
            return None
        if n > 1e11:                       # milliseconds
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    s = str(value).strip()
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime   # noqa: PLC0415
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            return None
    if dt is None:
        return None
    # A bare date parses to naive midnight; treat it as UTC rather than local
    # so the comparison does not drift by a timezone offset.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def filter_by_age(jobs: list[dict], days: int,
                  log: Callable[[str], None] | None = None, *,
                  verified_only: bool = False, review: list[dict] | None = None,
                  now: datetime | None = None) -> list[dict]:
    """Separate verified original publication from refresh/board evidence.

    Legacy broad mode keeps unknown dates, with an explicit warning. Strict
    mode sends unknown or non-original evidence to review, never the shortlist.
    A prior observation can disprove freshness but cannot prove publication.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(0, days))
    kept, dropped, uncertain = [], 0, 0
    for j in jobs:
        dt = parse_posted(j.get("posted"))
        first_seen = parse_posted(j.get("first_seen_at"))
        original = j.get("posted_kind") in {
            "employer_first_published", "employer_date_posted"}
        # A private ATS draft may have been created months before publication.
        public_date = dt if j.get("posted_kind") != "employer_created" else None
        if days > 0 and ((public_date and public_date < cutoff) or (first_seen and first_seen < cutoff)):
            status, reason = "old", "Publication or prior observation predates the search window."
        elif dt and dt > now:
            status, reason = "future", "Publication date is in the future; check the source."
        elif not dt:
            status, reason = "unknown", "No usable original publication date."
        elif not original:
            status, reason = "unverified", "Board, creation or last-publication date is not proof of original publication."
        else:
            status = "verified_fresh" if days > 0 else "unrestricted"
            reason = "Employer original publication date verified."
        j["freshness"] = {"status": status, "reason": reason}
        if status == "old":
            dropped += 1
        elif status == "future" or (verified_only and status in {"unknown", "unverified"}):
            uncertain += 1
            j.setdefault("review_reasons", []).append(reason)
            if review is not None:
                review.append(j)
        else:
            kept.append(j)
    if log:
        log(f"  freshness: excluded {dropped} old; {uncertain} require review")
    return kept


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


def _location_ok(job: dict, market: dict | None) -> tuple[bool, str]:
    """Location gate, driven entirely by a resolved market dict (see MARKETS).

    Returns (ok, reason_if_not). market is None when the phase asks for no
    location gate at all, in which case everything passes.
    """
    if not market:
        return True, ""

    loc  = src_mod.fold(job.get("location") or "").lower()
    body = src_mod.fold(job.get("description") or "").lower()
    is_remote = bool(job.get("remote")) or _any(loc, REMOTE_TERMS)

    # An explicit office obligation takes precedence over a board's city tag.
    # Mere mentions (customers/HQ/travel) do not imply a required office.
    office_cities = GERMANY_TERMS[2:-1] + ("münchen", "amsterdam", "london", "paris")
    required_cities = set()
    for city in office_cities:
        name = re.escape(city)
        patterns = (
            rf"(?:must|require[ds]?|expected|able to|based|work from).{{0,65}}\b{name}\s+office\b",
            rf"\b{name}\s+office\b.{{0,60}}(?:days? (?:a|per) week|times a week|onsite|on-site)",
            rf"(?:office|buro|standort)\s+(?:in\s+)?{name}\b.{{0,60}}(?:days? (?:a|per) week|tage|vor ort)",
        )
        if any(re.search(p, body) for p in patterns):
            required_cities.add(city)
    if required_cities:
        if market.get("require_remote"):
            return False, "location conflict: required office attendance is not fully remote"
        allowed = market.get("in_market", ())
        if not all(_any(city, allowed) for city in required_cities):
            return False, "location conflict: required office in " + ", ".join(sorted(required_cities))
        def city_aliases(city: str) -> tuple:
            munich = ("munich", "munchen", "münchen", "muenchen")
            return munich if city in munich else (city,)

        if loc and not all(_any(loc, city_aliases(city)) for city in required_cities):
            return False, "location conflict: advertised location differs from required office in " + ", ".join(sorted(required_cities))

    if market.get("require_remote") and job.get("workplace_type") in {"Hybrid", "OnSite"}:
        return False, "not remote: employer specifies " + job["workplace_type"]
    if market.get("in_market") == APPROVED_CITIES or market is MARKETS["frankfurt"]:
        if re.search(r"frankfurt\s*(?:\(\s*oder|an der oder)", loc):
            return False, "not an approved office destination: Frankfurt (Oder) is not Frankfurt am Main"

    # An explicit work-authorisation demand in the body overrides any
    #    location string, which is often just a head-office address.
    auth = next((t for t in market["work_auth_block"] if t and t in body), "")
    if auth:
        return False, f"needs work rights not held ('{auth}')"

    # Physically in the market, after contradictions and explicit obligations.
    if market["in_market"] and _any(loc, market["in_market"]):
        return True, ""

    # 3. Remote - but remote from where?
    #    Strip the arrangement words. Whatever survives is a place.
    residue = loc
    for term in REMOTE_GENERIC:
        residue = residue.replace(term, " ")
    residue = re.sub(r"[^a-z0-9]+", " ", residue).strip()
    # A JD that merely mentions remote work ("hybrid", "remote-friendly") does
    # not make a role with a named office remote: "Amsterdam office" or
    # "Munich, Bavaria, Germany" stays an on-site job. Only a location with no
    # office in it ("Germany", blank) can be settled by the body, and then only
    # by a plain statement that the role is remote and not hybrid.
    place = residue
    for term in _COUNTRY_LEVEL:
        place = re.sub(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", " ", place)
    place = re.sub(r"\s+", " ", place).strip()
    body_remote = (not place and _any(body[:1500], _REMOTE_STRONG)
                   and not _any(body[:1500], _ONSITE_HINT))
    if is_remote or body_remote:
        if _any(loc, market["reachable_remote"]):
            return True, ""
        # A JD that plainly says work-from-anywhere, HQ city notwithstanding.
        if _any(body[:900], _REMOTE_ANYWHERE):
            return True, ""
        explicit_remote_region = re.search(
            r"(?:remote (?:within|in|from|across)|work from anywhere in)\s+"
            r"(germany|deutschland|europe|the eu)\b", body)
        if explicit_remote_region and _any(explicit_remote_region.group(1), market["reachable_remote"]):
            return True, ""
        # Whatever survives did not match the reachable list, so it is a place
        # he cannot work from. Rejecting unknown regions by default beats
        # enumerating Earth.
        if residue:
            return False, (f"remote but region-locked: "
                           f"{job.get('location') or '?'}")
        # Nothing but arrangement words, or blank. Before calling it
        # unrestricted, fall back to the board's own market.
        home = SOURCE_DEFAULT_REGION.get(job.get("source", ""))
        if home and not _any(home.lower(), market["reachable_remote"]):
            return False, (f"{job.get('source')} is a {home} board and this "
                           f"posting names no region - assuming {home}")
        if market.get("require_remote_region_evidence"):
            return False, "location conflict: remote country eligibility is not stated"
        return True, ""

    # 4. Not in the market and not remote.
    if market.get("require_remote"):
        return False, f"not remote: {job.get('location') or '?'}"
    return False, (f"not reachable for {market['label']}: "
                   f"{job.get('location') or '?'}")


def prefilter(job: dict, market: "dict | str | None" = None,
              cfg: dict | None = None) -> dict:
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
    hw = next((t for t in TITLE_REJECT_HARDWARE if _has(title, t)), "")
    if hw:
        reject.append(f"hardware description role ('{hw}'): {job['title']}")
    hit = next((t for t in TITLE_REJECT_DISCIPLINE if _has(title, t)), "")
    if hit and not _any(title, SOFTWARE_TITLE_TERMS):
        # "Embedded Software Engineer, RF Systems" is still a software role, so
        # an explicit software word in the title overrides the discipline hit.
        reject.append(f"non-software discipline ('{hit.strip('* ')}'): "
                      f"{job['title']}")

    opening = body[:1200]
    for t in BODY_LEVEL_REJECT:
        if _has(opening, t):
            reject.append(f"body advertises a junior/entry role ('{t}')")
            break

    # No language in a software JD is not evidence of a different mandatory
    # stack. Conversely an explicit Ruby/Rails specialist is not 'agnostic'.
    preferred = _any(blob, REQUIRE_ANY)
    other_languages = ("ruby", "rails", "php", "laravel", "java", "kotlin",
                       "golang", "go", "rust", "typescript", "javascript",
                       "node.js", "swift", "scala", "elixir", "dart")
    # Optional exposure is not a prescribed stack. Keep bare 'go' out of
    # business prose unless it has programming/experience context.
    required_other = False
    prescribed_other = False
    for sentence in re.split(r"[\n;.!?,]+\s*|\b(?:but|whereas|while)\b", blob):
        matches = [t for t in other_languages if _has(sentence, t)]
        go_context = (r"\bgo\b.{0,30}(?:experience|programming|development)|"
                      r"(?:experience|proficiency|knowledge|expert).{0,30}\bgo\b")
        if matches == ["go"] and not re.search(go_context, sentence):
            continue
        optional = bool(re.search(
            r"optional|nice to have|a plus|bonus|not required|not essential", sentence))
        if matches and not optional:
            prescribed_other = True
            mandatory = bool(re.search(
                r"\bmust\b|\brequired\b|\bexpert\b|\bessential\b|\bmandatory\b", sentence))
            alternatives = bool(re.search(r"\b(?:or|any)\b|such as|for example", sentence))
            if mandatory and not _any(sentence, REQUIRE_ANY) and not alternatives:
                required_other = True
    language_flexible = bool(re.search(
        r"language[- ]agnostic|(?:any|one or more|at least one) (?:modern )?(?:programming )?language|"
        r"(?:more than|rather than|regardless of) (?:a |the |any )?(?:specific )?language|"
        r"no (?:specific )?language (?:requirement|required)|"
        r"not (?:tied|limited) to (?:a |any )?(?:specific )?language", blob))
    if re.search(r"(?:not (?:a |an )?|isn.t |not considered )language[- ]agnostic", blob):
        language_flexible = False
    stack_match = ("other" if required_other else "preferred" if preferred else
                   "agnostic" if language_flexible or not prescribed_other else "other")
    if stack_match == "agnostic":
        if (cfg or {}).get("allow_stack_agnostic", True):
            notes.append("Stack-agnostic software role: no prescribed different language; assess engineering fit.")
        else:
            reject.append("stack-agnostic roles disabled")
    elif stack_match == "other":
        reject.append("different explicit stack; not language-agnostic")

    why_excluded = exclusion_reason(job, cfg)
    if why_excluded:
        reject.append(why_excluded)

    if market is not None:
        if isinstance(market, str):
            market = resolve_market(market)
        ok, why = _location_ok(job, market)
        if not ok:
            reject.append(why)

    clearance = next((t for t in CLEARANCE_BLOCK if _has(blob, t)), "")
    if clearance:
        reject.append(f"security clearance ('{clearance.rstrip('*')}')")

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
        "stack_match": stack_match,
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


def _dedupe_key(job: dict) -> tuple[str, str, str]:
    title = _NOISE.sub(" ", src_mod.fold(job.get("title", "")).lower())
    title = re.sub(r"\b(?:snr|sr)\b", "senior", title)
    company = _NOISE.sub(" ", src_mod.fold(job.get("company", "")).lower())
    loc = src_mod.fold(job.get("location", "")).lower().replace("münchen", "munich")
    loc = _NOISE.sub(" ", loc)
    loc = re.sub(r"\b(?:germany|deutschland|german)\b", " ", loc)
    loc = re.sub(r"\b(?:munchen|muenchen)\b", "munich", loc)
    return (" ".join(title.split()), " ".join(company.split()), " ".join(loc.split()))


def dedupe(jobs: list[dict]) -> list[dict]:
    """Prefer original employer evidence before description length.

    Distinct seniority levels and office locations must not collapse into a
    single role. Retain alternate IDs so dismissals cover duplicate sources.
    """
    best: dict[tuple, dict] = {}

    def employer(j: dict) -> bool:
        return (str(j.get("posted_kind", "")).startswith("employer_") or
                j.get("source") in src_mod.ATS_ADAPTERS)

    def canonical(j: dict) -> str:
        try:
            parsed = urlsplit(j.get("url") or "")
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
            host = "job-boards.greenhouse.io"
        if host not in {"job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                        "job-boards.eu.greenhouse.io", "jobs.ashbyhq.com", "jobs.lever.co"}:
            return ""
        return host + parsed.path.rstrip("/")

    employer_ids: dict[tuple, set[str]] = {}
    canonical_ids: dict[str, str] = {}
    for j in jobs:
        if employer(j):
            employer_ids.setdefault(_dedupe_key(j), set()).add(j["id"])
            if canonical(j):
                canonical_ids[canonical(j)] = j["id"]
    all_employer_ids = {jid for ids in employer_ids.values() for jid in ids}

    def quality(j: dict) -> tuple[int, int]:
        kind = j.get("posted_kind")
        authority = (3 if kind in {"employer_first_published", "employer_date_posted"}
                     else 2 if employer(j) else 1)
        return authority, len(j.get("description") or "")

    for row in jobs:
        j = dict(row)
        metadata = _dedupe_key(j)
        if not metadata[0]:
            continue
        # A title/company/city is not a requisition ID. Only use fuzzy board
        # reconciliation when exactly one employer requisition can match.
        possible = employer_ids.get(metadata, set())
        identity = j["id"] if employer(j) else canonical_ids.get(canonical(j))
        if not identity:
            identity = (next(iter(possible)) if len(possible) == 1 and not canonical(j)
                        else canonical(j) or j["id"])
        if employer(j) or identity in all_employer_ids:
            k = ("employer", identity)
        elif canonical(j):
            k = ("url", canonical(j))
        else:
            k = metadata + (identity,)
        cur = best.get(k)
        if cur is None:
            best[k] = j
            continue
        winner, other = (j, cur) if quality(j) > quality(cur) else (cur, j)
        winner["also_on"] = list(set(winner.get("also_on", []) +
                                     other.get("also_on", []) + [other.get("source", "")]))
        winner["alternate_ids"] = sorted(set(winner.get("alternate_ids", []) +
                                              other.get("alternate_ids", []) + [other["id"]]))
        if other.get("posted_kind") == "board_posted":
            winner["board_posted"] = other.get("posted", "")
        elif other.get("board_posted"):
            winner["board_posted"] = other["board_posted"]
        observations = [d for d in (parse_posted(winner.get("first_seen_at")),
                                     parse_posted(other.get("first_seen_at"))) if d]
        if observations:
            winner["first_seen_at"] = min(observations).isoformat()
        best[k] = winner
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
                 registry_jobs: list[dict] | None = None,
                 tick: Callable[[str, int, int], None] | None = None,
                 market: dict | None = None,
                 ) -> list[dict]:
    """Fetch every source a phase asks for. Never raises for one bad source.

    registry_jobs lets the caller poll the company registry once and share it
    across phases - the ATS results are identical either way, and every phase
    re-polling them is pure duplicate traffic.

    market supplies the Seek `where` value and, for a phase that leaves a
    source list unset, its default sources.
    """
    jobs: list[dict] = []
    market = market or {}
    where  = market.get("where", "") or ""
    seek_sites = phase.get("seek_sites")
    if seek_sites is None:
        seek_sites = market.get("seek_sites", [])
    aggs = phase.get("aggregators")
    if aggs is None:
        aggs = market.get("aggregators", [])
    use_linkedin = phase.get("linkedin")
    if use_linkedin is None:
        use_linkedin = market.get("linkedin", False)
    # Seek and Indeed can filter by age server-side, which saves paging through
    # stale results; every other source is filtered on the way out below.
    max_age = int(cfg.get("max_age_days", 0) or 0)

    # A phase is minutes of work, so reporting only "phase 1 of 2" leaves the
    # progress bar frozen and looking crashed. Report the sub-steps instead.
    def step(msg: str, done: int = 0, total: int = 0) -> None:
        if tick:
            tick(msg, done, total)

    if phase.get("use_registry", True) and registry:
        if registry_jobs is None:
            registry_jobs = src_mod.fetch_registry(
                registry, on_source=lambda n, c: log(f"    registry {n}: {c}"))
        got = [dict(j) for j in registry_jobs]
        log(f"  registry: {len(got)}")
        jobs.extend(got)

    for site in seek_sites:
        # Collect every keyword's hits first and de-duplicate by posting id
        # before hydrating. Keywords overlap heavily - a senior C++ embedded
        # role matches four of them - and hydration is one HTTP call per
        # posting, so deduplicating first is the difference between ~450 calls
        # and ~150 for the same result.
        per_kw: list[list[dict]] = []
        seen_ids: set[str] = set()
        kws = phase.get("keywords", [])
        for n, kw in enumerate(kws, 1):
            step(f"Searching Seek for \"{kw}\"", n - 1, len(kws))
            try:
                stubs = src_mod.seek_search(
                    kw, site=site, where=where,
                    max_jobs=phase.get("max_jobs", 300),
                    date_range=max_age or 31)
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
            # The slowest part of a scan by far - one request per posting - so
            # it is the part that most needs to visibly move.
            todo = src_mod.seek_hydrate(
                todo, site=site,
                progress=lambda d, t: step(
                    f"Reading job descriptions from Seek", d, t))
        jobs.extend(todo)

    if use_linkedin:
        # Same shape as Seek: search every keyword, de-duplicate, triage on the
        # title, and only then pay for one detail request per survivor.
        seen_li: set[str] = set()
        fresh_li: list[dict] = []
        kws = phase.get("keywords", [])
        for n, kw in enumerate(kws, 1):
            step(f"Searching LinkedIn for \"{kw}\"", n - 1, len(kws))
            try:
                stubs = src_mod.linkedin_search(
                    kw, where=where, days=max_age, max_jobs=phase.get("linkedin_max", 100),
                    max_pages=phase.get("linkedin_pages", 4))
            except Exception as exc:                             # noqa: BLE001
                log(f"  linkedin '{kw}': failed ({exc})")
                continue
            # LinkedIn pads a city search with nearby and region-wide roles, and
            # it cannot be trusted on remote, so only a posting whose location
            # names the market's own place is kept.
            places = market.get("in_market") or ()
            stubs = [j for j in stubs if not places or _any(
                src_mod.fold(j.get("location") or "").lower(), places)]
            keep = [j for j in stubs if triage_title(j)
                    and not exclusion_reason(dict(j, description=""), cfg)]
            new = [j for j in keep if j["id"] not in seen_li]
            seen_li.update(j["id"] for j in new)
            log(f"  linkedin '{kw}': {len(stubs)} found, "
                f"{len(keep)} past triage, {len(new)} new")
            fresh_li.extend(new)
        todo = fresh_li[: phase.get("linkedin_hydrate", 150)]
        if todo:
            log(f"  linkedin: reading {len(todo)} job descriptions")
            todo = src_mod.linkedin_hydrate(
                todo, progress=lambda d, t: step(
                    "Reading job descriptions from LinkedIn", d, t))
        jobs.extend(todo)

    if aggs:
        done = [0]

        def agg_done(name: str, count: int) -> None:
            done[0] += 1
            log(f"    {name}: {count}")
            step(f"Reading remote job feeds ({name})", done[0], len(aggs))

        step("Reading remote job feeds", 0, len(aggs))
        got = src_mod.fetch_aggregators(aggs, on_source=agg_done)
        log(f"  aggregators: {len(got)}")
        jobs.extend(got)

    profiles = phase.get("browser_profiles")
    if profiles is None:
        profiles = market.get("browser_profiles", [])
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
                pages = phase.get("browser_pages", 4)
                step(f"Opening a browser for {', '.join(profiles)}", 0, pages)
                try:
                    got = render.fetch_profiles(
                        profiles, kw, max_jobs=phase.get("max_jobs", 100),
                        max_pages=pages, since_days=max_age,
                        headless=cfg.get("headless", True),
                        engine=cfg.get("browser_engine", "rotate"),
                        device=cfg.get("browser_device", "desktop"),
                        on_page=lambda site, n, fresh, total: step(
                            f"Reading {site} page {n} ({total} jobs so far)",
                            n, pages))
                except Exception as exc:                         # noqa: BLE001
                    log(f"  browser '{kw}': failed ({exc})")
                    continue
                log(f"  browser '{kw}': {len(got)}")
                jobs.extend(got)
            if len(jobs) == before:
                log("  browser returned nothing - JSON sources still apply")

    # Apply freshness after cross-source dedupe in run_scan. Filtering an old
    # employer original here would let a newer board repost conceal its age.

    for j in jobs:
        j.setdefault("phase", phase.get("name", market.get("label", "")))
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

    # Short numeric ids. Board ids are long slugs ("arbeitnow:senior-...-berlin-
    # 12345") that the model mangles when echoing them back, and a mangled id
    # silently left the job unranked while its rank number was lost.
    alias = {str(n): j for n, j in enumerate(jobs, 1)}
    rows = []
    for n, j in enumerate(jobs, 1):
        llm = j.get("llm", {}) or {}
        matched = "; ".join(
            f"{k}:{','.join(v[:5])}" for k, v in (j.get("hits") or {}).items())
        rows.append(
            f"id={n}\n"
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
    placed: list[tuple[int, dict]] = []
    for row in order:
        key = str(row.get("id", "")).strip()
        j = alias.get(key) or by_id.get(key)
        if j and "rank" not in j:
            j["rank"]    = row.get("rank")
            j["verdict"] = row.get("verdict", "")
            j["why"]     = row.get("why", "")
            placed.append((int(row.get("rank") or 10**6), j))
    ranked = [j for _, j in sorted(placed, key=lambda x: x[0])]
    # Anything the model skipped goes below the ranked ones, best fit first,
    # rather than floating with no number.
    rest = sorted((j for j in jobs if "rank" not in j),
                  key=lambda x: -float(x.get("llm_score") or 0))
    ordered = ranked + rest
    # Renumber densely so the list always reads 1..N with no gaps.
    for n, j in enumerate(ordered, 1):
        j["rank"] = n
    return ordered


# ══════════════════════════════════════════════════════════════════════════════
# Scan
# ══════════════════════════════════════════════════════════════════════════════

def run_scan(prefilter_min: float | None = None, limit: int | None = None,
             do_llm: bool = True, do_rank: bool = False,
             include_seen: bool = True, since_days: int | None = None,
             progress: Callable[[str, int, int], None] | None = None,
             market: str | None = None,
             verified_only: bool | None = None,
             ) -> dict:
    """Full scan across every enabled phase, persisted to disk.

    market, when given, replaces the configured phases with a single on-demand
    phase for that market (see MARKETS) - this is how "scan Germany now" works
    without editing the saved configuration. An unknown name raises ValueError.

    include_seen defaults to True: a posting you have not acted on is still a
    live opportunity, and hiding it was what made rescans look empty. Dismissed
    and applied postings are always excluded.

    since_days limits results to postings newer than that many days, overriding
    the config's max_age_days. This is the other half of the same problem: with
    seen postings shown, a scan is dominated by the same long-lived listings
    every time, and a fresh-only scan is how you see just what appeared since.
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
    if since_days is not None:
        cfg = dict(cfg, max_age_days=int(since_days))
    if verified_only is None:
        verified_only = bool(cfg.get("require_verified_posted", True))
    max_age = int(cfg.get("max_age_days", 0) or 0)
    if max_age > 0:
        log(f"freshness: postings from the last {max_age} day(s) only")

    registry = load_sources()
    state    = _load_state()
    seen     = state["seen"]
    hidden   = set(state["dismissed"]) | set(state["applied"])

    forced_market: dict | None = None
    if market:
        forced_market = resolve_market(market, cfg)     # raises on a typo
        phases = [_synthetic_phase(market.strip().lower(), forced_market, cfg)]
        log(f"market override: scanning {forced_market['label']} only")
    else:
        phases = [p for p in cfg.get("phases", []) if p.get("enabled", True)]

    # Resolve each phase's market up front so a bad name is reported before any
    # network traffic, and the same object is reused for gather + prefilter.
    phase_markets: list[dict | None] = []
    for p in phases:
        try:
            phase_markets.append(forced_market or resolve_market(p, cfg))
        except ValueError as exc:
            raise ValueError(f"{exc} in phase {p.get('name', '?')!r}") from exc

    all_jobs: list[dict] = []
    survivors: list[dict] = []
    rejected = 0
    per_phase: list[dict] = []

    # Poll the registry once and share it - the phases differ in how they gate
    # results, not in what the ATS boards return.
    registry_jobs: list[dict] | None = None
    if registry and any(p.get("use_registry", True) for p in phases):
        done = [0]

        def reg_done(name: str, count: int) -> None:
            done[0] += 1
            tick(f"Reading company career pages ({name})",
                 done[0], len(registry))

        tick("Reading company career pages", 0, len(registry))
        registry_jobs = src_mod.fetch_registry(registry, on_source=reg_done)
        log(f"registry: {len(registry_jobs)} postings from "
            f"{len(registry)} companies")

    for idx, (phase, mkt) in enumerate(zip(phases, phase_markets), 1):
        name = phase.get("name", f"phase {idx}")
        tick(f"{name}: starting", 0, 1)
        log(f"\n[{idx}/{len(phases)}] {name}"
            + (f"  (market: {mkt['label']})" if mkt else ""))

        raw = gather_phase(phase, cfg, registry, log, registry_jobs, tick, mkt)
        for j in raw:
            if j["id"] in seen:
                j["first_seen_at"] = seen[j["id"]]
        all_jobs.extend(raw)
        per_phase.append({"name": name, "fetched": len(raw), "kept": 0})

    # Dedupe across phases and sources, then rank by the cheap score first so
    # the LLM budget goes to the most promising postings.
    candidates = dedupe(all_jobs)
    review_jobs: list[dict] = []
    eligible: list[dict] = []
    for j in candidates:
        if hidden.intersection([j["id"]] + j.get("alternate_ids", [])):
            continue
        if not include_seen and any(i in seen for i in [j["id"]] + j.get("alternate_ids", [])):
            continue
        evaluations = [prefilter(j, mkt, cfg) for mkt in phase_markets]
        accepted = next(((i, result) for i, result in enumerate(evaluations)
                         if not result["reject"]), None)
        conflict = False
        if accepted is None:
            # Contradictory office evidence is visible for human checking, but
            # never sent to the LLM or advertised as an eligible location.
            accepted = next(((i, r) for i, r in enumerate(evaluations)
                             if r["reject"] and all("location conflict:" in why
                                                    for why in r["reject"])), None)
            conflict = accepted is not None
        if accepted is None:
            rejected += 1
            continue
        idx, evaluation = accepted
        j.update(evaluation)
        if j["prefilter_score"] < prefilter_min:
            continue
        j["phase"] = per_phase[idx]["name"]
        per_phase[idx]["kept"] += 1
        if conflict:
            j["review_reasons"] = list(j["reject"])
        eligible.append(j)

    dated = filter_by_age(eligible, max_age, log, verified_only=verified_only,
                          review=review_jobs)
    for j in dated:
        if j.get("review_reasons"):
            review_jobs.append(j)
        else:
            survivors.append(j)
    freshness_excluded = sum(j.get("freshness", {}).get("status") == "old"
                             for j in eligible)

    for j in survivors:
        j["is_new"] = j["id"] not in seen
    def ordering(j: dict) -> tuple:
        city = src_mod.fold(j.get("location") or "").lower()
        return (not j.get("is_new", False), not _has(city, "berlin"), -j["prefilter_score"])

    survivors.sort(key=ordering)
    review_jobs.sort(key=lambda j: -j.get("prefilter_score", 0))
    tick(f"Checking {len(survivors)} jobs against your CV",
         len(survivors), len(survivors))

    shortlist = survivors[:limit]
    if do_llm and shortlist:
        _, _, config = _ai_bits()
        if config.load_resume_text(config.load()).strip():
            llm_score(shortlist, verbose=False,
                      progress=lambda d, t: tick(
                          "AI reading job descriptions", d, t))
            if do_rank and len(shortlist) > 1:
                tick("AI putting them in order", 0, len(shortlist))
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
            "review": len(review_jobs), "freshness_excluded": freshness_excluded,
            "new": sum(1 for j in slim if j.get("is_new")),
        },
        "phases": per_phase,
        "jobs": slim,
        "review_jobs": [{k: v for k, v in j.items() if k != "description"}
                        for j in review_jobs],
    }
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_PATH.write_text(json.dumps(payload, indent=2))
    tick(f"Done - {len(slim)} jobs on your list", 1, 1)
    return payload


def load_last() -> dict:
    if LAST_PATH.exists():
        try:
            return json.loads(LAST_PATH.read_text())
        except ValueError:
            pass
    return {"generated": "", "counts": {}, "phases": [], "jobs": [], "review_jobs": []}


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
                    help="skip the comparative ranking pass (the default)")
    ap.add_argument("--rank", action="store_true",
                    help="rank the shortlist comparatively; needs a large "
                         "model, a small one grades everything strong")
    ap.add_argument("--limit", type=int, default=None,
                    help="max postings sent to the LLM stage")
    ap.add_argument("--phase", default="",
                    help="run only the phase whose name contains this")
    ap.add_argument("--market", default="",
                    help="scan one market on demand instead of the configured "
                         "phases: " + ", ".join(sorted(MARKETS)))
    ap.add_argument("--list-markets", action="store_true",
                    help="print the known markets and exit")
    ap.add_argument("--since-days", type=int, default=None, metavar="N",
                    help="only postings from the last N days (0 = no limit)")
    ap.add_argument("--fresh", action="store_true",
                    help="only what was posted in the last 24h "
                         "(same as --since-days 1)")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="explicit broad mode: allow unknown/board/refresh dates")
    ap.add_argument("--check-url", default="",
                    help="inspect original publication evidence for a public Greenhouse/Ashby job; no scan or state writes")
    ap.add_argument("--validate", action="store_true",
                    help="check which registry sources respond, then exit")
    ap.add_argument("--discover", default="",
                    help="find a company's ATS board from its website")
    ap.add_argument("--add", action="store_true",
                    help="with --discover, append hits to sources.json")
    ap.add_argument("-o", "--out", default="",
                    help="write the report here (default: stdout)")
    args = ap.parse_args(argv)

    if args.check_url:
        try:
            posting = src_mod.verify_posting_url(args.check_url)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if posting is None:
            print("No active public posting found, or source unavailable.", file=sys.stderr)
            return 1
        review: list[dict] = []
        filter_by_age([posting], args.since_days if args.since_days is not None else 7,
                      verified_only=True, review=review)
        print(json.dumps({k: v for k, v in posting.items() if k != "description"}, indent=2))
        return 0

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

    if args.list_markets:
        cfg = load_config()
        for name in sorted(set(MARKETS) | set(cfg.get("markets") or {})):
            m = resolve_market(name, cfg)
            sites = ",".join(m.get("seek_sites") or []) or "-"
            print(f"  {name:<12} {m['label']:<20} "
                  f"seek:{sites:<7} aggregators:{len(m.get('aggregators') or [])}"
                  f"  remote-only:{'yes' if m.get('require_remote') else 'no'}")
        return 0

    if args.validate:
        for s in load_sources():
            ok, n = src_mod.validate_source(s)
            print(f"  {'ok' if ok else '--'} {s.get('ats',''):<16} "
                  f"{s.get('name', s.get('slug','')):<24} {n:>4}")
        return 0

    cfg = load_config()
    if args.phase and not args.market:
        for p in cfg["phases"]:
            p["enabled"] = args.phase.lower() in p.get("name", "").lower()
        save_config(cfg)

    since = 1 if args.fresh else args.since_days
    try:
        payload = run_scan(prefilter_min=args.prefilter_min, limit=args.limit,
                           do_llm=not args.prefilter_only,
                           do_rank=args.rank and not args.no_rank, since_days=since,
                           market=args.market or None,
                           verified_only=False if args.allow_unverified else None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    c = payload["counts"]
    print(f"\nfetched {c['fetched']}, rejected {c['rejected']}, "
          f"survivors {c['survivors']}, shortlist {c['shortlist']} "
          f"({c['new']} new)", file=sys.stderr)
    print(f"review queue: {c.get('review', 0)}; old excluded: "
          f"{c.get('freshness_excluded', 0)}", file=sys.stderr)

    text = report(payload["jobs"], args.min_score, args.prefilter_only)
    if payload.get("review_jobs"):
        text += "\n\n# Requires verification (not the active shortlist)\n\n"
        text += report(payload["review_jobs"], 0, True)
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
