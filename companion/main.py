"""
grapply — companion service

Start with:
    pip install -r requirements.txt
    python main.py

Or with custom port:
    python main.py --port 7878

Endpoints:
    GET  /health    — liveness probe used by the extension
    POST /generate  — build prompt, call AI, return cover letter (md + html)
    POST /save      — write cover_letter.md + cover_letter.html to disk
    POST /extract   — adaptive job extraction (cached parser or LLM-generated)
    POST /fill      — adaptive ATS filler generation (cached JSON ops or LLM-generated)
"""

from __future__ import annotations

import argparse
import hashlib
import html as _html
import logging
import re
import sys
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import markdown as md_lib
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import analyzer as analyzer_module
import ats_filler as ats_filler_module
import config as cfg_module
import ai_client as ai_module

# ── Boot ─────────────────────────────────────────────────────────────────────

_LOG_PATH = Path("~/.grapply/companion.log").expanduser()
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

import threading as _threading
_cfg_lock = _threading.Lock()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    force=True,  # prevent uvicorn from adding duplicate handlers
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_PATH, mode="a"),
    ],
)
# Suppress noisy low-level HTTP transport loggers — only our own code should emit DEBUG
for _noisy in (
    "httpcore", "httpcore.http11", "httpcore.http2",
    "httpcore.connection", "httpcore.connection_pool",
    "httpcore.proxy", "httpcore.socks",
    "httpx", "uvicorn.access",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CFG = cfg_module.load()
AI  = ai_module.AIClient(CFG["ai"])

app = FastAPI(title="grapply companion", version="1.0.0")

# Allow the browser extension (chrome-extension:// / moz-extension://) to call
# us.  Restricting to extension-scheme origins prevents arbitrary web pages from
# using the companion as a file-write or AI-proxy primitive.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://[\w-]+|moz-extension://[\w-]+",
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ── Auth ─────────────────────────────────────────────────────────────────────

def _require_token(x_grapply_token: Optional[str] = Header(default=None)) -> None:
    """Reject requests that don't carry the configured shared secret.

    Only enforced when ``auth_token`` is set in config.toml.  Leave it empty
    during initial setup; set it to a random string once you're ready to lock
    things down, then copy the same value to the extension's Settings.
    """
    expected = CFG.get("auth_token", "")
    if expected and x_grapply_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Grapply-Token")


# ── Request / Response models ─────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    job_title:            str
    company:              str
    job_description:      str
    resume_text:          str = ""
    user_profile:         dict          = Field(default_factory=dict)
    global_instructions:  str           = ""
    per_job_instructions: str           = ""
    easter_egg:           bool          = False
    easter_egg_text:      Optional[str] = None
    # Optional per-request AI override from extension settings.
    # When provided these take precedence over the companion's config.toml.
    ai_provider:          Optional[str] = None
    ai_endpoint:          Optional[str] = None
    ai_model:             Optional[str] = None
    ai_key:               Optional[str] = None


class GenerateResponse(BaseModel):
    cover_letter_md:   str
    cover_letter_html: str


class SaveRequest(BaseModel):
    company:           str
    role:              str
    cover_letter_md:   str
    cover_letter_html: str
    job_description:   str = ""
    resume_text:       str = ""
    domain:            str = ""
    ai_provider:       str = ""
    ai_model:          str = ""
    # output_dir intentionally removed from the public API — the companion
    # always writes under its configured output_dir to prevent arbitrary
    # file writes by any caller.


class SaveResponse(BaseModel):
    md_path:   str
    html_path: str
    job_id:    str


def _format_summary(data: dict, company: str, domain: str) -> str:
    """Format research_company() result as summary.md."""
    lines = [f"# Company Research — {company}", ""]
    if data.get("error"):
        lines += [f"> ⚠️ Could not fetch company data: {data['error']}", ""]
        lines += ["---", "*Generated by grapply*"]
        return "\n".join(lines)
    meta = []
    if data.get("industry"):
        meta.append(f"**Industry:** {data['industry']}")
    if data.get("size_stage"):
        meta.append(f"**Size/Stage:** {data['size_stage']}")
    if meta:
        lines.append("  ".join(meta))
    lines.append(f"**Generated:** {date.today()}  **Source:** {domain}")
    lines.append("")
    if data.get("overview"):
        lines += ["## Overview", data["overview"], ""]
    if data.get("products_services"):
        lines += ["## Products & Services"] + [f"- {p}" for p in data["products_services"]] + [""]
    if data.get("tech_stack_hints"):
        lines += ["## Tech Stack Hints"] + [f"- {t}" for t in data["tech_stack_hints"]] + [""]
    if data.get("culture_signals"):
        lines += ["## Culture Signals"] + [f"- {s}" for s in data["culture_signals"]] + [""]
    if data.get("mission_statement"):
        lines += ["## Mission", f'> {data["mission_statement"]}', ""]
    if data.get("red_flags"):
        lines += ["## 🚩 Red Flags"] + [f"- {f}" for f in data["red_flags"]] + [""]
    if data.get("green_flags"):
        lines += ["## ✅ Green Flags"] + [f"- {f}" for f in data["green_flags"]] + [""]
    if data.get("notable"):
        lines += ["## Notable", data["notable"], ""]
    lines += ["---", "*Generated by grapply*"]
    return "\n".join(lines)


def _format_score(data: dict, role: str, company: str) -> str:
    """Format score_job_fit() result as score.md."""
    score = data.get("score", 0)
    rec = data.get("recommendation", "Unknown")
    rec_emoji = {"Apply": "✅", "Apply with caveats": "⚠️", "Stretch role": "🔶", "Skip": "❌"}.get(rec, "")
    lines = [f"# Job Fit — {role} at {company}", ""]
    if data.get("error"):
        lines += [f"> ⚠️ Could not score: {data['error']}", ""]
        lines += ["---", "*Generated by grapply*"]
        return "\n".join(lines)
    lines += [f"## Score: {score}/10 — {rec} {rec_emoji}", ""]
    meta = []
    if data.get("experience_gap"):
        meta.append(f"**Experience gap:** {data['experience_gap']}")
    overq = data.get("overqualified_risk")
    if overq is not None:
        meta.append(f"**Overqualified risk:** {'Yes' if overq else 'No'}")
    meta.append(f"**Generated:** {date.today()}")
    lines += ["  ".join(meta), ""]
    if data.get("matching_skills"):
        lines += ["## ✅ Matching Skills"] + [f"- {s}" for s in data["matching_skills"]] + [""]
    if data.get("missing_skills"):
        lines += ["## ❌ Missing Skills"] + [f"- {s}" for s in data["missing_skills"]] + [""]
    if data.get("reasoning"):
        lines += ["## Honest Assessment", data["reasoning"], ""]
    lines += ["---", "*Generated by grapply*"]
    return "\n".join(lines)


def _format_insight(data: dict, role: str, company: str) -> str:
    """Format decode_jargon() result as insight.md."""
    verdict = data.get("verdict", "")
    verdict_emoji = {"Apply": "✅", "Apply with caution": "⚠️", "Skip": "❌"}.get(verdict, "")
    lines = [f"# Role Insights — {role} at {company}", ""]
    if verdict:
        lines += [f"**Verdict:** {verdict} {verdict_emoji}  **Generated:** {date.today()}", ""]
    if data.get("overall_vibe"):
        lines += [f"> {data['overall_vibe']}", ""]
    red = data.get("red_flags", [])
    green = data.get("green_flags", [])
    if red:
        lines += [
            "## 🚩 Red Flags",
            "",
            "| Phrase | What it really means |",
            "|--------|---------------------|",
        ]
        for f in red:
            lines.append(f"| {f.get('phrase', '')} | {f.get('reality', '')} |")
        lines.append("")
    if green:
        lines += [
            "## ✅ Green Flags",
            "",
            "| Phrase | What it signals |",
            "|--------|----------------|",
        ]
        for f in green:
            lines.append(f"| {f.get('phrase', '')} | {f.get('signal', '')} |")
        lines.append("")
    lines += ["---", "*Generated by grapply*"]
    return "\n".join(lines)


def _append_error(dest: Path, msg: str) -> None:
    """Append an error line to _errors.log so the extension can report failures."""
    try:
        with open(dest / "_errors.log", "a", encoding="utf-8") as f:
            f.write(f"{date.today().isoformat()} {msg}\n")
    except OSError:
        pass


def _bg_write_summary(dest: Path, domain: str, company: str) -> None:
    logger.info("[analysis] summary.md starting — fetching %s", domain)
    try:
        data = analyzer_module.research_company(domain, company, AI)
        (dest / "summary.md").write_text(_format_summary(data, company, domain), encoding="utf-8")
        logger.info("[analysis] summary.md done for %s", company)
    except Exception as exc:
        logger.warning("[analysis] summary.md failed: %s", exc)
        _append_error(dest, f"summary.md failed: {exc}")


def _populate_profile_from_resume() -> None:
    """Extract profile fields from the resume PDF and patch config.toml.

    Called once at startup when a resume path is configured but profile
    fields (name/email/phone) are still empty. Runs synchronously before
    uvicorn starts so the in-memory CFG is updated before any requests arrive.
    """
    resume_text = cfg_module.load_resume_text(CFG)
    if not resume_text:
        logger.warning("[profile] resume text is empty — cannot auto-populate profile")
        return

    print("  Extracting profile from resume (one-time setup)...", flush=True)
    system = (
        "You are a resume parser. Extract ONLY explicitly stated contact and profile "
        "information from the resume text. Do NOT infer, guess, or construct any value "
        "that is not literally present in the text.\n\n"
        "Return ONLY valid JSON with these exact keys:\n"
        '  name, email, phone, linkedin, github, website, location, '
        'work_authorization, availability, salary_expectation\n\n'
        "Rules:\n"
        "- Use \"\" for any field not explicitly found in the text.\n"
        "- linkedin/github/website: only include if a URL or handle is literally written.\n"
        "- work_authorization: only include if the resume explicitly states citizenship, "
        "visa status, or right-to-work (e.g. 'Open Work Visa', 'PR', 'requires sponsorship'). "
        "Never assume citizenship from a name or location.\n"
        "- location: city/country only if stated (e.g. 'Auckland, New Zealand').\n"
        "- Do not include any explanation — JSON only."
    )
    try:
        raw = AI.generate(system, resume_text[:6000])
        # Extract JSON from response (model may wrap in markdown)
        import re, json as _json
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            logger.warning("[profile] LLM returned no JSON: %r", raw[:200])
            return
        data: dict = _json.loads(m.group())
    except Exception as exc:
        logger.warning("[profile] profile extraction failed: %s", exc)
        return

    # Only keep string values; drop empty ones so we don't overwrite non-empty fields
    updates = {k: str(v).strip() for k, v in data.items() if v and str(v).strip()}
    if not updates:
        logger.warning("[profile] LLM returned empty profile data")
        return

    cfg_module.patch_profile_in_toml(updates)
    # Update in-memory config so this session benefits immediately
    for k, v in updates.items():
        CFG.setdefault("profile", {})[k] = v

    filled = ", ".join(f"{k}={repr(v)}" for k, v in updates.items())
    print(f"  ✅ Profile populated: {filled}", flush=True)
    logger.info("[profile] auto-populated: %s", filled)


def _bg_write_score(dest: Path, job_description: str, resume_text: str, role: str, company: str) -> None:
    logger.info("[analysis] score.md starting for %s @ %s", role, company)
    try:
        data = analyzer_module.score_job_fit(job_description, resume_text, AI)
        (dest / "score.md").write_text(_format_score(data, role, company), encoding="utf-8")
        logger.info("[analysis] score.md done — score=%s recommendation=%s", data.get("score"), data.get("recommendation"))
    except Exception as exc:
        logger.warning("[analysis] score.md failed: %s", exc)
        _append_error(dest, f"score.md failed: {exc}")


def _bg_write_insight(dest: Path, job_description: str, role: str, company: str) -> None:
    logger.info("[analysis] insight.md starting for %s @ %s", role, company)
    try:
        data = analyzer_module.decode_jargon(job_description, AI)
        (dest / "insight.md").write_text(_format_insight(data, role, company), encoding="utf-8")
        logger.info("[analysis] insight.md done — verdict=%s red_flags=%d",
                    data.get("verdict"), len(data.get("red_flags", [])))
    except Exception as exc:
        logger.warning("[analysis] insight.md failed: %s", exc)
        _append_error(dest, f"insight.md failed: {exc}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    ai_ok, ai_model = AI.health_check()
    ats_filler_module.FILLERS_DIR.mkdir(parents=True, exist_ok=True)
    fillers_cached = sum(1 for _ in ats_filler_module.FILLERS_DIR.glob("*.json"))
    warnings = cfg_module.get_setup_warnings(CFG)
    if not ai_ok:
        warnings.insert(0, f"AI unreachable at {AI.endpoint} — is Ollama running?")
    return {
        "status":         "ok",
        "ai_provider":     AI.provider,
        "ai_model":        ai_model,
        "ai_endpoint":     AI.endpoint,
        "ai_reachable":    ai_ok,
        "fillers_cached":  fillers_cached,
        "profile":         CFG.get("profile", {}),
        "setup_warnings":  warnings,
    }


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest, _: None = Depends(_require_token)):
    # Build a per-request AIClient when the extension overrides provider/model/
    # endpoint/key; otherwise reuse the global client to avoid the overhead of
    # constructing a new one on every request.
    ai = AI
    if req.ai_provider or req.ai_model or req.ai_endpoint:
        override = dict(CFG["ai"])
        if req.ai_provider: override["provider"] = req.ai_provider
        if req.ai_endpoint: override["endpoint"] = req.ai_endpoint.rstrip("/")
        if req.ai_model:    override["model"]    = req.ai_model
        if req.ai_key:      override["api_key"]  = req.ai_key
        ai = ai_module.AIClient(override)

    if not req.resume_text:
        req = req.model_copy(update={"resume_text": cfg_module.load_resume_text(CFG)})

    logger.info("[generate] %s @ %s | resume=%d chars | jd=%d chars",
                req.job_title, req.company, len(req.resume_text), len(req.job_description))
    system_prompt = _build_system_prompt()
    user_prompt   = _build_user_prompt(req)

    try:
        logger.info("[generate] calling AI (%s %s)...", ai.provider, ai.model)
        raw = ai.generate(system_prompt, user_prompt, _endpoint="generate")
        logger.info("[generate] AI done — response %d chars", len(raw))
    except ai_module.AIError as exc:
        logger.error("[generate] AI error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    # Normalise - some models wrap output in markdown fences
    cover_md = _unwrap_fences(raw)
    cover_md = _clean_output(cover_md)

    # Append easter egg HTML comment if requested
    if req.easter_egg:
        egg_text = req.easter_egg_text or CFG["cover_letter"]["easter_egg_text"]
        # Inject company/role into the egg text if placeholders exist
        egg_text = egg_text.replace("{company}", req.company).replace("{role}", req.job_title)
        comment_lines = "\n".join(f"  {l}" for l in egg_text.splitlines())
        cover_md += f"\n\n<!--\n{comment_lines}\n-->\n"

    cover_html = _md_to_html(cover_md, req.company, req.job_title)

    return GenerateResponse(cover_letter_md=cover_md, cover_letter_html=cover_html)


@app.post("/save", response_model=SaveResponse)
def save(req: SaveRequest, background_tasks: BackgroundTasks, _: None = Depends(_require_token)):
    out_root = Path(CFG["output_dir"]).expanduser()
    company  = _safe_name(req.company)
    role     = _safe_name(req.role)
    # Resolve effective provider/model - prefer request fields, fall back to
    # companion config so the path is always populated even for older clients.
    provider = _safe_name(req.ai_provider or CFG["ai"]["provider"] or "unknown")
    model    = _safe_name(req.ai_model    or CFG["ai"]["model"]    or "unknown")
    dest     = out_root / company / role / provider / model
    dest.mkdir(parents=True, exist_ok=True)

    md_path   = dest / "cover_letter.md"
    html_path = dest / "cover_letter.html"

    md_path.write_text(req.cover_letter_md, encoding="utf-8")
    html_path.write_text(req.cover_letter_html, encoding="utf-8")
    logger.info("[save] cover_letter written → %s", dest)

    resume_text = req.resume_text or cfg_module.load_resume_text(CFG)

    if req.job_description:
        logger.info("[save] queuing insight.md background task")
        background_tasks.add_task(_bg_write_insight, dest, req.job_description, req.role, req.company)
        if resume_text:
            logger.info("[save] queuing score.md background task")
            background_tasks.add_task(_bg_write_score, dest, req.job_description, resume_text, req.role, req.company)
        else:
            logger.info("[save] no resume_text — skipping score.md")
    if req.domain:
        logger.info("[save] queuing summary.md background task (domain=%s)", req.domain)
        background_tasks.add_task(_bg_write_summary, dest, req.domain, req.company)
    else:
        logger.info("[save] no domain — skipping summary.md")

    job_id = hashlib.blake2b(str(dest).encode(), digest_size=6).hexdigest()
    return SaveResponse(md_path=str(md_path), html_path=str(html_path), job_id=job_id)


# ── /jobs/{job_id}/files ──────────────────────────────────────────────────────

@app.get("/jobs/{job_id}/files")
def job_files(job_id: str, _: None = Depends(_require_token)):
    """Poll which analysis files have been written for a given job_id."""
    out_root = Path(CFG["output_dir"]).expanduser()
    dest: Path | None = None
    if out_root.exists():
        # New structure: company/role/provider/model (4 levels)
        # Legacy structure: company/role (2 levels) — still supported
        for path in out_root.rglob("cover_letter.md"):
            candidate = path.parent
            if hashlib.blake2b(str(candidate).encode(), digest_size=6).hexdigest() == job_id:
                dest = candidate
                break
    if dest is None:
        raise HTTPException(status_code=404, detail=f"job_id {job_id!r} not found")
    return {
        "job_id": job_id,
        "cover_letter": (dest / "cover_letter.md").exists(),
        "summary": (dest / "summary.md").exists(),
        "score": (dest / "score.md").exists(),
        "insight": (dest / "insight.md").exists(),
    }


# ── /jobs/recent ─────────────────────────────────────────────────────────────

@app.get("/jobs/recent")
def jobs_recent(limit: int = 5, _: None = Depends(_require_token)):
    """Return the most recently saved jobs from the output directory.

    Each entry has: title, company, cover_letter_md, saved_at (ISO timestamp).
    Used by the extension to seed its savedJobs storage after a reload.
    """
    out_root = Path(CFG["output_dir"]).expanduser()
    entries = []
    if out_root.exists():
        for company_dir in out_root.iterdir():
            if not company_dir.is_dir():
                continue
            for role_dir in company_dir.iterdir():
                cl = role_dir / "cover_letter.md"
                if not cl.exists():
                    continue
                entries.append({
                    "company":          company_dir.name,
                    "title":            role_dir.name.replace("-", " "),
                    "cover_letter_md":  cl.read_text(encoding="utf-8"),
                    "saved_at":         cl.stat().st_mtime,
                })
    entries.sort(key=lambda e: e["saved_at"], reverse=True)
    # Convert mtime float to ISO string for the client
    for e in entries[:limit]:
        import datetime
        e["saved_at"] = datetime.datetime.fromtimestamp(e["saved_at"]).isoformat()
    return {"jobs": entries[:limit]}


# ── /extract ──────────────────────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    domain:    str = Field(..., description="Hostname, e.g. 'linkedin.com'")
    page_text: str = Field(default="", description="document.body.innerText fallback")
    page_html: str = Field(default="", description="Full outerHTML -- companion cleans it")
    url:       str = Field(default="", description="Full page URL for ATS path detection")
    # Pre-extracted fields from client-side DOM parsing (JSON-LD, meta, title heuristic).
    # Used as a fast path when HTML cleaning fails (React SPAs, lazy-loaded content).
    pre_title:       str = Field(default="")
    pre_company:     str = Field(default="")
    pre_location:    str = Field(default="")
    pre_description: str = Field(default="")


class ScanRequest(BaseModel):
    domain:    str
    page_html: str = Field(default="")
    url:       str = Field(default="")


class ScanResponse(BaseModel):
    mode: str   # "job_posting" | "ats_form"


class BenchmarkExtractRequest(BaseModel):
    domain:    str = Field(default="", description="Hostname for catalog logging")
    page_html: str = Field(...,        description="Raw page HTML to benchmark strategies against")


class ExtractResponse(BaseModel):
    title:       str = ""
    company:     str = ""
    description: str = ""
    location:    str = ""
    mode:        str = "job_posting"   # included so extension can trust companion's decision


class FillRequest(BaseModel):
    domain:        str
    form_snapshot: list[dict] = Field(default_factory=list)
    fill_data:     dict = Field(default_factory=dict)


class FillResponse(BaseModel):
    operations: list[dict]
    cached: bool


@app.post("/scan", response_model=ScanResponse)
def scan_page(req: ScanRequest, _: None = Depends(_require_token)):
    """Lightweight mode detection -- no LLM, just HTML/domain analysis.

    Returns {"mode": "job_posting"} or {"mode": "ats_form"} instantly.
    Call this first; only call /extract when mode == "job_posting".
    """
    import extractor
    mode = extractor.detect_mode(req.page_html, req.domain, req.url)
    logger.info("[scan] domain=%s -> %s", req.domain, mode)
    return ScanResponse(mode=mode)


@app.post("/extract", response_model=ExtractResponse)
def extract_job(req: ExtractRequest, _: None = Depends(_require_token)):
    import extractor
    logger.info("[extract] domain=%s html=%d chars text=%d chars",
                req.domain, len(req.page_html), len(req.page_text))
    mode   = extractor.detect_mode(req.page_html, req.domain, req.url)
    pre    = {
        "title":       req.pre_title,
        "company":     req.pre_company,
        "location":    req.pre_location,
        "description": req.pre_description,
    }
    result = extractor.extract(req.domain, req.page_text, AI, page_html=req.page_html, pre_extracted=pre)
    result["mode"] = mode
    logger.info("[extract] result: title=%r company=%r parser_cached=%s",
                result.get("title", ""), result.get("company", ""),
                bool((extractor.PARSERS_DIR / f"{req.domain}.py").exists()))
    return ExtractResponse(**result)


@app.post("/benchmark/extract")
def benchmark_extract(req: BenchmarkExtractRequest, _: None = Depends(_require_token)):
    """Benchmark all HTML cleaning strategies, log results to DB catalog."""
    import extractor
    return extractor.benchmark_html(req.page_html, domain=req.domain)


@app.get("/benchmark/catalog")
def benchmark_catalog(domain: str = ""):
    """Return per-domain strategy catalog from DB. ?domain=nz.seek.com for one domain."""
    import tracker
    return tracker.get_strategy_catalog(domain or None)


_MAX_FILL_VALUE_LEN = 2000  # Truncate fill_data values to prevent prompt injection bloat


@app.post("/fill", response_model=FillResponse)
def fill_form(req: FillRequest, _: None = Depends(_require_token)):
    # Sanitize fill_data: truncate values to prevent oversized prompt injection
    clean_fill = {str(k)[:100]: str(v)[:_MAX_FILL_VALUE_LEN] for k, v in req.fill_data.items()}
    logger.info("[fill] domain=%s fields=%d", req.domain, len(req.form_snapshot))
    operations = ats_filler_module.get_filler(req.domain, req.form_snapshot, AI, clean_fill)
    if not operations:
        raise HTTPException(status_code=422, detail="Could not generate a filler for this form")
    cached = ats_filler_module.last_cache_hit()
    logger.info("[fill] returning filler for %s (cached=%s)", req.domain, cached)
    return FillResponse(operations=operations, cached=cached)


@app.get("/parsers")
def list_parsers_endpoint(_: None = Depends(_require_token)):
    from tool_server import list_parsers
    return list_parsers()


@app.delete("/parsers/{domain}")
def delete_parser_endpoint(domain: str, _: None = Depends(_require_token)):
    from tool_server import delete_parser
    result = delete_parser(domain)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    token = CFG.get("auth_token", "")
    return HTMLResponse(content=_SETTINGS_HTML.replace("%%AUTH_TOKEN%%", token))


@app.get("/config")
def get_config(_: None = Depends(_require_token)):
    data = cfg_module.get_config_for_ui(CFG)
    max_words = CFG["cover_letter"].get("max_words", 450)
    language  = CFG["cover_letter"].get("language", "English")
    if not data["cover_letter"]["system_instructions"]:
        data["cover_letter"]["system_instructions"] = _default_system_prompt(max_words, language)
    return data


class ConfigUpdate(BaseModel):
    ai: dict = {}
    resume: dict = {}
    cover_letter: dict = {}


@app.post("/config")
def update_config(req: ConfigUpdate, _: None = Depends(_require_token)):
    section_updates: dict = {}

    allowed_ai = {"provider", "model", "endpoint", "api_key", "timeout", "tool_timeout"}
    allowed_resume = {"path"}
    allowed_cover = {"max_words", "language", "system_instructions", "user_instructions",
                     "humanize", "easter_egg_enabled"}

    if req.ai:
        section_updates["ai"] = {k: v for k, v in req.ai.items() if k in allowed_ai}
    if req.resume:
        section_updates["resume"] = {k: v for k, v in req.resume.items() if k in allowed_resume}
    if req.cover_letter:
        section_updates["cover_letter"] = {k: v for k, v in req.cover_letter.items() if k in allowed_cover}

    cfg_module.patch_config_bulk(section_updates)

    # Reload the in-memory config so changes take effect immediately
    global CFG, AI
    with _cfg_lock:
        CFG = cfg_module.load()
        AI  = ai_module.AIClient(CFG["ai"])

    return {"ok": True}


@app.get("/stats", response_class=HTMLResponse)
def stats_page():
    token = CFG.get("auth_token", "")
    return HTMLResponse(content=_STATS_HTML.replace("%%AUTH_TOKEN%%", token))


@app.get("/token-stats/daily")
def token_stats_daily_endpoint(
    days: int = 30,
    provider: str = "",
    endpoint: str = "",
):
    import tracker
    return tracker.get_token_daily(days=days, provider=provider, endpoint=endpoint)


@app.get("/token-stats")
def token_stats_endpoint(
    provider: str = "",
    model: str = "",
    endpoint: str = "",
    days: int = 0,
    _: None = Depends(_require_token),
):
    import tracker
    return tracker.get_token_stats(
        provider=provider, model=model, endpoint=endpoint, days=days
    )


@app.delete("/fillers/{domain}")
def delete_filler_endpoint(domain: str, _: None = Depends(_require_token)):
    from ats_filler import FILLERS_DIR
    from tool_server import _safe_domain
    path = FILLERS_DIR / f"{_safe_domain(domain)}.json"
    if path.exists():
        path.unlink()
        return {"deleted": domain}
    raise HTTPException(status_code=404, detail=f"No filler for {domain!r}")


# ── Settings page HTML ───────────────────────────────────────────────────────

_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>grapply - Settings</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    :root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;--ok:#4caf7d;--danger:#e05252;--text:#e8eaf0;--muted:#7b7f96}
    body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
    header{padding:20px 32px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:16px}
    .logo{font-weight:700;font-size:20px;letter-spacing:-.3px}.logo span{color:var(--accent)}
    .subtitle{color:var(--muted);font-size:13px}
    main{max-width:760px;margin:0 auto;padding:28px 24px 60px}
    section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:22px 24px;margin-bottom:20px}
    h2{font-size:13px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--border)}
    .field{margin-bottom:14px}
    .field:last-child{margin-bottom:0}
    label{display:block;font-size:12px;font-weight:500;color:var(--muted);margin-bottom:5px}
    input[type=text],input[type=password],input[type=number],select,textarea{
      width:100%;background:#0f1117;border:1px solid var(--border);border-radius:6px;
      color:var(--text);font-size:13px;padding:8px 10px;outline:none;font-family:inherit}
    input:focus,select:focus,textarea:focus{border-color:var(--accent)}
    textarea{resize:vertical;line-height:1.5}
    small{display:block;color:var(--muted);font-size:11px;margin-top:4px}
    .mode-row{display:flex;gap:12px;margin-bottom:16px}
    .mode-btn{flex:1;padding:9px;border-radius:7px;border:1px solid var(--border);background:var(--bg);
      color:var(--muted);cursor:pointer;font-size:13px;font-weight:500;transition:all .15s;text-align:center}
    .mode-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(108,99,255,.08)}
    .provider-row{display:flex;gap:10px;margin-bottom:14px}
    .provider-btn{flex:1;padding:8px;border-radius:7px;border:1px solid var(--border);background:var(--bg);
      color:var(--muted);cursor:pointer;font-size:12px;font-weight:500;transition:all .15s;text-align:center}
    .provider-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(108,99,255,.08)}
    .hidden{display:none}
    .running-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(76,175,125,.1);border:1px solid rgba(76,175,125,.3);border-radius:6px;padding:5px 10px;font-size:11px;color:var(--ok);margin-bottom:14px}
    .running-badge .dot{width:6px;height:6px;border-radius:50%;background:var(--ok);flex-shrink:0}
    .save-bar{position:fixed;bottom:0;left:0;right:0;background:var(--surface);border-top:1px solid var(--border);
      padding:14px 24px;display:flex;align-items:center;gap:12px}
    .btn-save{background:var(--accent);color:#fff;border:none;border-radius:7px;padding:9px 28px;
      font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
    .btn-save:hover{opacity:.85}
    .btn-save:disabled{opacity:.5;cursor:not-allowed}
    .status{font-size:12px;color:var(--muted)}
    .status.ok{color:var(--ok)}.status.err{color:var(--danger)}
    select option{background:#1a1d27}
  </style>
</head>
<body>
<header>
  <div class="logo">gr<span>apply</span></div>
  <span class="subtitle">Settings</span>
</header>
<main>

  <!-- Resume -->
  <section>
    <h2>Resume / CV</h2>
    <div class="field">
      <label>Path to resume file (PDF, MD, or TXT)</label>
      <input type="text" id="resume-path" placeholder="/home/user/Documents/resume.pdf">
      <small>Full path on this machine. Used for cover letter generation and profile extraction.</small>
    </div>
  </section>

  <!-- AI Model -->
  <section>
    <h2>AI Model</h2>

    <div id="running-badge" class="running-badge hidden">
      <span class="dot"></span>
      <span id="running-model">Loading...</span>
    </div>

    <div class="mode-row">
      <button class="mode-btn" id="btn-online" onclick="setMode('online')">Online (Cloud)</button>
      <button class="mode-btn" id="btn-offline" onclick="setMode('offline')">Offline (Local)</button>
    </div>

    <!-- Online -->
    <div id="online-opts" class="hidden">
      <div class="provider-row">
        <button class="provider-btn" id="btn-openai" onclick="setOnlineProvider('openai')">OpenAI</button>
        <button class="provider-btn" id="btn-claude" onclick="setOnlineProvider('claude')">Anthropic / Claude</button>
      </div>
      <div class="field">
        <label>Model</label>
        <select id="model-preset" onchange="onPresetChange()"></select>
      </div>
      <div class="field hidden" id="custom-model-field">
        <label>Custom model name</label>
        <input type="text" id="custom-model" placeholder="e.g. gpt-4o-2024-11-20">
      </div>
      <div class="field">
        <label>API Key</label>
        <input type="password" id="api-key" placeholder="sk-... or sk-ant-...">
      </div>
    </div>

    <!-- Offline -->
    <div id="offline-opts" class="hidden">
      <div class="provider-row">
        <button class="provider-btn" id="btn-ollama" onclick="setOfflineProvider('ollama')">Ollama</button>
        <button class="provider-btn" id="btn-llamacpp" onclick="setOfflineProvider('llamacpp')">llama.cpp server</button>
      </div>
      <div class="field">
        <label>Endpoint URL</label>
        <input type="text" id="endpoint" placeholder="http://localhost:8080">
      </div>
      <div class="field">
        <label>Model name</label>
        <input type="text" id="offline-model" placeholder="llama3.2 or model.gguf">
        <small>For llama.cpp: use the filename of the GGUF (without path). For Ollama: use the model tag.</small>
      </div>
    </div>
  </section>

  <!-- Cover Letter -->
  <section>
    <h2>Cover Letter</h2>
    <div class="field" style="display:flex;gap:14px">
      <div style="flex:1">
        <label>Max words</label>
        <input type="number" id="max-words" min="100" max="1000" step="50">
      </div>
      <div style="flex:1">
        <label>Language</label>
        <input type="text" id="language" placeholder="English">
      </div>
    </div>
    <div class="field">
      <label>System instructions</label>
      <small>How the AI should write cover letters. Pre-filled with the built-in anti-detection prompt.</small>
      <textarea id="system-instructions" rows="18" style="margin-top:6px;font-size:12px"></textarea>
    </div>
    <div class="field">
      <label>Default user instructions</label>
      <small>Applied to every cover letter by default (e.g. "I need an offer letter for visa purposes").</small>
      <textarea id="user-instructions" rows="4" style="margin-top:6px" placeholder="Optional - leave empty for none"></textarea>
    </div>
    <div class="field" style="margin-top:12px;display:flex;gap:24px;flex-wrap:wrap">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="chk-humanize"> Enable Humanizer
        <small style="color:#888;margin-left:4px">(anti-AI-detection, local LLM only)</small>
      </label>
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="chk-easter-egg"> Include AI easter egg
        <small style="color:#888;margin-left:4px">(hidden prompt injection comment)</small>
      </label>
    </div>
  </section>

</main>

<div class="save-bar">
  <button class="btn-save" id="btn-save" onclick="saveSettings()">Save Settings</button>
  <span class="status" id="status"></span>
</div>

<script>
const TOKEN = '%%AUTH_TOKEN%%';
const H = Object.assign({'Content-Type':'application/json'}, TOKEN ? {'X-Grapply-Token':TOKEN} : {});

const ONLINE_PROVIDERS = ['openai', 'claude'];
const OFFLINE_PROVIDERS = ['ollama', 'llamacpp'];

const MODEL_PRESETS = {
  openai: [
    {id:'gpt-4o-mini',   label:'GPT-4o mini'},
    {id:'gpt-4o',        label:'GPT-4o'},
    {id:'gpt-4.1',       label:'GPT-4.1'},
    {id:'gpt-4.1-mini',  label:'GPT-4.1 mini'},
    {id:'gpt-4.1-nano',  label:'GPT-4.1 nano'},
    {id:'o1-mini',       label:'o1 mini'},
    {id:'o3-mini',       label:'o3 mini'},
    {id:'o4-mini',       label:'o4 mini'},
    {id:'__custom__',    label:'Custom model...'},
  ],
  claude: [
    {id:'claude-haiku-4-5-20251001', label:'Claude Haiku 4.5'},
    {id:'claude-sonnet-4-5',         label:'Claude Sonnet 4.5'},
    {id:'claude-sonnet-4-6',         label:'Claude Sonnet 4.6'},
    {id:'claude-opus-4-7',           label:'Claude Opus 4.7'},
    {id:'__custom__',                label:'Custom model...'},
  ],
};

const DEFAULT_ENDPOINTS = {
  openai: 'https://api.openai.com',
  claude: 'https://api.anthropic.com',
  ollama: 'http://localhost:11434',
  llamacpp: 'http://localhost:8080',
};

let state = {
  mode: 'offline',
  onlineProvider: 'openai',
  offlineProvider: 'llamacpp',
};

function setMode(m) {
  state.mode = m;
  document.getElementById('btn-online').classList.toggle('active', m === 'online');
  document.getElementById('btn-offline').classList.toggle('active', m === 'offline');
  document.getElementById('online-opts').classList.toggle('hidden', m !== 'online');
  document.getElementById('offline-opts').classList.toggle('hidden', m !== 'offline');
}

function setOnlineProvider(p) {
  state.onlineProvider = p;
  document.getElementById('btn-openai').classList.toggle('active', p === 'openai');
  document.getElementById('btn-claude').classList.toggle('active', p === 'claude');
  populatePresets(p);
}

function setOfflineProvider(p) {
  state.offlineProvider = p;
  document.getElementById('btn-ollama').classList.toggle('active', p === 'ollama');
  document.getElementById('btn-llamacpp').classList.toggle('active', p === 'llamacpp');
  const ep = document.getElementById('endpoint');
  if (!ep.value || Object.values(DEFAULT_ENDPOINTS).includes(ep.value)) {
    ep.value = DEFAULT_ENDPOINTS[p] || '';
  }
}

function populatePresets(provider, currentModel) {
  const sel = document.getElementById('model-preset');
  const presets = MODEL_PRESETS[provider] || [];
  sel.innerHTML = presets.map(p => `<option value="${p.id}">${p.label}</option>`).join('');
  const match = presets.find(p => p.id === currentModel);
  if (match) {
    sel.value = currentModel;
  } else if (currentModel) {
    sel.value = '__custom__';
    document.getElementById('custom-model').value = currentModel;
    document.getElementById('custom-model-field').classList.remove('hidden');
  } else {
    sel.value = presets[0]?.id || '';
  }
  onPresetChange();
}

function onPresetChange() {
  const isCustom = document.getElementById('model-preset').value === '__custom__';
  document.getElementById('custom-model-field').classList.toggle('hidden', !isCustom);
}

function getSelectedModel() {
  const preset = document.getElementById('model-preset').value;
  if (preset === '__custom__') return document.getElementById('custom-model').value.trim();
  return preset;
}

async function load() {
  try {
    const [cfgRes, healthRes] = await Promise.all([
      fetch('/config', {headers: H}),
      fetch('/health').catch(() => null),
    ]);
    if (!cfgRes.ok) { setStatus('Failed to load config: ' + cfgRes.status, true); return; }
    const cfg = await cfgRes.json();

    let runningModel = '';
    if (healthRes?.ok) {
      const h = await healthRes.json();
      runningModel = h.ai_model || '';
      if (runningModel) {
        document.getElementById('running-model').textContent = 'Running: ' + runningModel;
        document.getElementById('running-badge').classList.remove('hidden');
      }
    }

    document.getElementById('resume-path').value = cfg.resume?.path || '';
    document.getElementById('max-words').value = cfg.cover_letter?.max_words || 450;
    document.getElementById('language').value = cfg.cover_letter?.language || 'English';
    document.getElementById('system-instructions').value = cfg.cover_letter?.system_instructions || '';
    document.getElementById('user-instructions').value = cfg.cover_letter?.user_instructions || '';
    document.getElementById('chk-humanize').checked = !!cfg.cover_letter?.humanize;
    document.getElementById('chk-easter-egg').checked = !!cfg.cover_letter?.easter_egg_enabled;

    const provider = cfg.ai?.provider || 'llamacpp';
    const model    = cfg.ai?.model    || '';
    const endpoint = cfg.ai?.endpoint || '';
    const apiKey   = cfg.ai?.api_key  || '';

    document.getElementById('api-key').value  = apiKey;
    document.getElementById('endpoint').value = endpoint;

    if (ONLINE_PROVIDERS.includes(provider)) {
      setMode('online');
      setOnlineProvider(provider);
      populatePresets(provider, model);
    } else {
      setMode('offline');
      setOfflineProvider(provider);
      document.getElementById('offline-model').value = runningModel || model;
      if (endpoint) document.getElementById('endpoint').value = endpoint;
    }
  } catch(e) {
    setStatus('Error loading settings: ' + e.message, true);
  }
}

function setStatus(msg, isErr) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status' + (isErr ? ' err' : ' ok');
  if (!isErr) setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 3000);
}

async function saveSettings() {
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  setStatus('Saving...', false);

  const provider = state.mode === 'online' ? state.onlineProvider : state.offlineProvider;
  const model    = state.mode === 'online'
    ? getSelectedModel()
    : document.getElementById('offline-model').value.trim();
  const endpoint = state.mode === 'online'
    ? (DEFAULT_ENDPOINTS[provider] || '')
    : document.getElementById('endpoint').value.trim();

  const payload = {
    ai: {
      provider,
      model,
      endpoint,
      api_key: document.getElementById('api-key').value.trim(),
    },
    resume: {
      path: document.getElementById('resume-path').value.trim(),
    },
    cover_letter: {
      max_words:           parseInt(document.getElementById('max-words').value) || 450,
      language:            document.getElementById('language').value.trim() || 'English',
      system_instructions: document.getElementById('system-instructions').value,
      user_instructions:   document.getElementById('user-instructions').value,
      humanize:            document.getElementById('chk-humanize').checked,
      easter_egg_enabled:  document.getElementById('chk-easter-egg').checked,
    },
  };

  try {
    const r = await fetch('/config', {method:'POST', headers:H, body:JSON.stringify(payload)});
    if (r.ok) {
      setStatus('Settings saved.', false);
    } else {
      const t = await r.text();
      setStatus('Save failed: ' + t, true);
    }
  } catch(e) {
    setStatus('Error: ' + e.message, true);
  }
  btn.disabled = false;
}

load();
</script>
</body>
</html>"""

# ── Stats page HTML ──────────────────────────────────────────────────────────

_STATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>grapply - Usage Stats</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    :root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;--ok:#4caf7d;--danger:#e05252;--text:#e8eaf0;--muted:#7b7f96}
    body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
    a{color:var(--accent);text-decoration:none}
    header{padding:20px 32px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:20px;flex-wrap:wrap}
    .logo{font-weight:700;font-size:20px;letter-spacing:-.3px}.logo span{color:var(--accent)}
    .subtitle{color:var(--muted);font-size:12px;flex:1}
    .time-filter{display:flex;gap:4px}
    .time-filter button{padding:6px 14px;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer;font-size:12px;font-weight:500;transition:all .15s}
    .time-filter button.active{background:var(--accent);border-color:var(--accent);color:#fff}
    .time-filter button:hover:not(.active){border-color:var(--accent);color:var(--text)}
    .refresh{font-size:11px;color:var(--muted);margin-left:8px}
    .main{padding:24px 32px;max-width:1280px;margin:0 auto}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:24px}
    .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
    .card-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:8px}
    .card-value{font-size:26px;font-weight:700;line-height:1}
    .card-value.accent{color:var(--accent)}.card-value.ok{color:var(--ok)}
    .card-sub{font-size:11px;color:var(--muted);margin-top:6px}
    .charts{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px;margin-bottom:24px}
    .chart-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
    .chart-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:14px}
    .insights{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px;margin-bottom:24px}
    .section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:14px}
    .insights-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
    .insight-item{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
    .insight-label{font-size:11px;color:var(--muted);margin-bottom:4px}
    .insight-value{font-size:14px;font-weight:600;color:var(--text)}
    .insight-value.accent{color:var(--accent)}.insight-value.ok{color:var(--ok)}
    .table-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
    td{padding:10px 12px;border-bottom:1px solid #1e2130}
    tr:last-child td{border-bottom:none}
    tr:hover td{background:#1e2130}
    .pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
    .pill-generate{background:#1a2b3a;color:#6c9fff}
    .pill-extract{background:#1a3a24;color:#4caf7d}
    .pill-analyze{background:#2b1a3a;color:#b06cff}
    .loading{display:flex;align-items:center;justify-content:center;padding:80px;color:var(--muted)}
    .empty-row td{text-align:center;color:var(--muted);padding:32px}
    @media(max-width:900px){.charts{grid-template-columns:1fr}}
  </style>
</head>
<body>
<header>
  <div>
    <div class="logo">gr<span>apply</span></div>
    <div class="subtitle">AI Usage &amp; Cost Dashboard</div>
  </div>
  <div class="time-filter">
    <button data-days="1">Today</button>
    <button data-days="7">7 days</button>
    <button data-days="30" class="active">30 days</button>
    <button data-days="0">All time</button>
  </div>
  <span class="refresh" id="refresh-label">auto-refresh 30s</span>
</header>

<div class="main">
  <div class="loading" id="loading">Loading stats...</div>
  <div id="content" style="display:none">

    <div class="cards">
      <div class="card">
        <div class="card-label">Total Calls</div>
        <div class="card-value accent" id="c-calls">0</div>
        <div class="card-sub" id="c-calls-sub"></div>
      </div>
      <div class="card">
        <div class="card-label">Total Tokens</div>
        <div class="card-value" id="c-tokens">0</div>
        <div class="card-sub" id="c-tokens-sub"></div>
      </div>
      <div class="card">
        <div class="card-label">Est. Cost (USD)</div>
        <div class="card-value ok" id="c-cost">$0.00</div>
        <div class="card-sub" id="c-cost-sub"></div>
      </div>
      <div class="card">
        <div class="card-label">Avg / Cover Letter</div>
        <div class="card-value" id="c-avg">-</div>
        <div class="card-sub">per generate call</div>
      </div>
      <div class="card">
        <div class="card-label">Parser Extractions</div>
        <div class="card-value" id="c-extract">0</div>
        <div class="card-sub">LLM calls (cache misses)</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-box">
        <div class="chart-title">Daily Cost (USD)</div>
        <canvas id="daily-chart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-title">Calls by Endpoint</div>
        <canvas id="endpoint-chart"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-title">Cost by Model</div>
        <canvas id="model-chart"></canvas>
      </div>
    </div>

    <div class="insights">
      <div class="section-title">Insights</div>
      <div class="insights-grid" id="insights-grid"></div>
    </div>

    <div class="table-box">
      <div class="section-title">Breakdown by Provider / Model / Endpoint</div>
      <table>
        <thead>
          <tr>
            <th>Provider</th><th>Model</th><th>Endpoint</th>
            <th>Calls</th><th>Prompt Tokens</th><th>Completion Tokens</th>
            <th>Est. Cost</th><th>Avg / Call</th>
          </tr>
        </thead>
        <tbody id="breakdown-body"></tbody>
      </table>
    </div>

  </div>
</div>

<script>
  const AUTH_TOKEN = '%%AUTH_TOKEN%%';
  const H = AUTH_TOKEN ? {'X-Grapply-Token': AUTH_TOKEN} : {};

  let dailyChart, endpointChart, modelChart;
  let currentDays = 30;

  Chart.defaults.color = '#7b7f96';
  Chart.defaults.borderColor = '#2a2d3a';
  Chart.defaults.font.family = "-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
  Chart.defaults.font.size = 11;

  const C = {
    accent:   '#6c63ff',
    ok:       '#4caf7d',
    generate: '#6c9fff',
    extract:  '#4caf7d',
    analyze:  '#b06cff',
    warn:     '#f0a050',
    danger:   '#e05252',
  };

  const fmt = n => n >= 1e6 ? (n/1e6).toFixed(1)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : String(n||0);
  const fmtC = n => n > 0 ? '$'+(n).toFixed(6).replace(/0+$/,'').replace(/\\.$/, '') : '$0';
  const fmtC4 = n => '$'+(n||0).toFixed(4);

  async function load(days) {
    document.getElementById('loading').style.display = 'flex';
    document.getElementById('content').style.display = 'none';
    const p = days ? '?days='+days : '';
    try {
      const [stats, daily] = await Promise.all([
        fetch('/token-stats'+p, {headers:H}).then(r=>r.json()),
        fetch('/token-stats/daily'+p, {headers:H}).then(r=>r.json()),
      ]);
      render(stats, daily);
      document.getElementById('loading').style.display = 'none';
      document.getElementById('content').style.display = '';
    } catch(e) {
      document.getElementById('loading').textContent = 'Error: '+e.message;
    }
  }

  function render(stats, daily) {
    const s = stats.summary || {};
    const bm = stats.by_model || [];

    // Cards
    const genRows = bm.filter(r=>r.endpoint==='generate');
    const extRows = bm.filter(r=>r.endpoint==='extract');
    const genCalls = genRows.reduce((a,r)=>a+r.calls,0);
    const genCost  = genRows.reduce((a,r)=>a+(r.cost_usd||0),0);
    const extCalls = extRows.reduce((a,r)=>a+r.calls,0);

    document.getElementById('c-calls').textContent = fmt(s.calls||0);
    document.getElementById('c-tokens').textContent = fmt(s.total_tokens||0);
    document.getElementById('c-cost').textContent = fmtC4(s.total_cost_usd||0);
    document.getElementById('c-avg').textContent = genCalls>0 ? fmtC(genCost/genCalls) : '-';
    document.getElementById('c-extract').textContent = fmt(extCalls);
    document.getElementById('c-calls-sub').textContent = genCalls+' generate, '+extCalls+' extract';
    document.getElementById('c-tokens-sub').textContent = fmt(s.prompt_tokens||0)+' prompt + '+fmt(s.completion_tokens||0)+' completion';
    document.getElementById('c-cost-sub').textContent = genCalls>0 ? fmtC4(genCost)+' on cover letters' : '';

    renderDailyChart(daily);
    renderEndpointChart(bm);
    renderModelChart(bm);
    renderInsights(s, bm, genCalls, genCost, extCalls);
    renderTable(bm);
  }

  function renderDailyChart(daily) {
    if (dailyChart) dailyChart.destroy();
    const ctx = document.getElementById('daily-chart').getContext('2d');
    if (!daily.length) {
      ctx.canvas.parentNode.innerHTML = '<div style="color:var(--muted);text-align:center;padding:40px 0;font-size:12px">No data for this period</div>';
      return;
    }
    const labels = daily.map(d=>d.date);
    dailyChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {label:'Cost (USD)', data:daily.map(d=>d.cost_usd||0), borderColor:C.accent,
           backgroundColor:C.accent+'22', fill:true, tension:.3,
           pointRadius:labels.length>20?2:4, yAxisID:'y'},
          {label:'Calls', data:daily.map(d=>d.calls||0), borderColor:C.ok,
           backgroundColor:'transparent', tension:.3, borderDash:[5,4],
           pointRadius:labels.length>20?2:4, yAxisID:'y1'},
        ],
      },
      options: {
        responsive:true,
        interaction:{mode:'index',intersect:false},
        plugins:{legend:{position:'top',labels:{boxWidth:10,padding:12}}},
        scales:{
          x:{grid:{color:'#1e2130'}},
          y:{grid:{color:'#1e2130'},ticks:{callback:v=>'$'+v.toFixed(4)}},
          y1:{position:'right',grid:{drawOnChartArea:false},ticks:{callback:v=>v+' calls'}},
        },
      },
    });
  }

  function renderEndpointChart(bm) {
    if (endpointChart) endpointChart.destroy();
    const epMap = {};
    for (const r of bm) epMap[r.endpoint] = (epMap[r.endpoint]||0)+r.calls;
    const labels = Object.keys(epMap);
    if (!labels.length) return;
    const ctx = document.getElementById('endpoint-chart').getContext('2d');
    endpointChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels,
        datasets:[{
          data: labels.map(l=>epMap[l]),
          backgroundColor: labels.map(l=>C[l]||C.accent),
          borderColor:'#1a1d27', borderWidth:3,
        }],
      },
      options:{
        responsive:true,
        plugins:{
          legend:{position:'bottom',labels:{padding:12,boxWidth:10}},
          tooltip:{callbacks:{label:c=>' '+c.label+': '+c.parsed+' calls'}},
        },
      },
    });
  }

  function renderModelChart(bm) {
    if (modelChart) modelChart.destroy();
    const mMap = {};
    for (const r of bm) mMap[r.model] = (mMap[r.model]||0)+(r.cost_usd||0);
    const entries = Object.entries(mMap).sort((a,b)=>b[1]-a[1]);
    if (!entries.length) return;
    const ctx = document.getElementById('model-chart').getContext('2d');
    const palette = [C.accent, C.ok, C.analyze, C.warn, C.danger];
    modelChart = new Chart(ctx, {
      type:'bar',
      data:{
        labels: entries.map(([m])=>m.length>22?m.slice(0,20)+'...':m),
        datasets:[{
          label:'Cost (USD)',
          data: entries.map(([,v])=>v),
          backgroundColor: entries.map((_,i)=>palette[i%palette.length]),
          borderRadius:4,
        }],
      },
      options:{
        indexAxis:'y',
        responsive:true,
        plugins:{legend:{display:false}},
        scales:{
          x:{grid:{color:'#1e2130'},ticks:{callback:v=>'$'+v.toFixed(4)}},
          y:{grid:{color:'#1e2130'}},
        },
      },
    });
  }

  function renderInsights(s, bm, genCalls, genCost, extCalls) {
    const items = [];

    items.push({label:'Total spend', value:fmtC4(s.total_cost_usd||0), cls:'ok'});
    items.push({label:'Cover letters generated', value:String(genCalls)});
    items.push({label:'Parser extractions (LLM)', value:String(extCalls),
      note: extCalls>0?'Each future visit uses cached parser (free)':''});

    if (genCalls>0)
      items.push({label:'Avg cost / cover letter', value:fmtC(genCost/genCalls)});

    // Most used model
    const mCalls = {};
    for (const r of bm) mCalls[r.model]=(mCalls[r.model]||0)+r.calls;
    const topM = Object.entries(mCalls).sort((a,b)=>b[1]-a[1])[0];
    if (topM) items.push({label:'Most used model', value:topM[0], cls:'accent'});

    // Best value (lowest avg cost/call, min 2 calls)
    const mCost={}, mN={};
    for (const r of bm) {
      mCost[r.model]=(mCost[r.model]||0)+(r.cost_usd||0);
      mN[r.model]=(mN[r.model]||0)+r.calls;
    }
    const candidates = Object.keys(mCost).filter(m=>mN[m]>=2);
    if (candidates.length>1) {
      const best = candidates.map(m=>({m, avg:mCost[m]/mN[m]})).sort((a,b)=>a.avg-b.avg)[0];
      items.push({label:'Best value model', value:best.m+' ('+fmtC(best.avg)+'/call)'});
    }

    if ((s.calls||0)>0)
      items.push({label:'Avg tokens / call', value:fmt(Math.round((s.total_tokens||0)/(s.calls||1)))});

    const analyzeRows = bm.filter(r=>r.endpoint==='analyze');
    const analyzeCalls = analyzeRows.reduce((a,r)=>a+r.calls,0);
    if (analyzeCalls>0) items.push({label:'Analysis calls', value:String(analyzeCalls)});

    document.getElementById('insights-grid').innerHTML = items.map(i=>`
      <div class="insight-item">
        <div class="insight-label">${i.label}</div>
        <div class="insight-value${i.cls?' '+i.cls:''}">${i.value}</div>
        ${i.note?`<div class="insight-label" style="margin-top:4px">${i.note}</div>`:''}
      </div>`).join('');
  }

  function renderTable(bm) {
    const tbody = document.getElementById('breakdown-body');
    if (!bm.length) {
      tbody.innerHTML='<tr class="empty-row"><td colspan="8">No data for this period</td></tr>';
      return;
    }
    tbody.innerHTML = bm.map(r=>`
      <tr>
        <td>${r.provider}</td>
        <td style="font-family:monospace;font-size:11px">${r.model}</td>
        <td><span class="pill pill-${r.endpoint}">${r.endpoint}</span></td>
        <td>${r.calls}</td>
        <td>${fmt(r.prompt_tokens||0)}</td>
        <td>${fmt(r.completion_tokens||0)}</td>
        <td>${fmtC4(r.cost_usd||0)}</td>
        <td>${fmtC(r.cost_usd/r.calls)}</td>
      </tr>`).join('');
  }

  // Time filter
  document.querySelectorAll('.time-filter button').forEach(btn=>{
    btn.addEventListener('click',()=>{
      document.querySelectorAll('.time-filter button').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      currentDays = +btn.dataset.days;
      load(currentDays);
    });
  });

  // Auto-refresh
  let countdown = 30;
  setInterval(()=>{
    countdown--;
    document.getElementById('refresh-label').textContent = 'auto-refresh '+countdown+'s';
    if (countdown<=0) { countdown=30; load(currentDays); }
  }, 1000);

  load(currentDays);
</script>
</body>
</html>"""


# ── Prompt builders ───────────────────────────────────────────────────────────

_HZR = (
    "eNpdVE1v4zYQvftXDHxJAjhCN9tFgdy82RwCpAngxFn0OBZHFhuKFMiRHfXX941sp+5e9MHhzLz3"
    "5pEvr6v13et6tXyk1frx/oUumxRC2pN8cK1hvLqdfanojfNIRaJKrIWCxK225DJ3rL7mEMaKlop1Lk"
    "q6T59bCw3RSaY/aJ+yK2e7UhRKO4Ruvp1iT2L/2maRswKpoeI7Hzif+vpITDntq9lNRS/KWU9gp6q"
    "fMPcem+fL6OaUMs2/D3r4eEnzava1oodYh8HJ/5J7zkhvZaJFXDzil2h4Wi+GKBM3CqhMjkt7Vc1+"
    "r+j5kMzbzH1LpU1DcLQRigmlhDeoj8WsE0OOeIA/HrnQ9S+SdfxRzb5V9CNZNkl0ZMqMZ/WTSVA0p"
    "7ilOhmP4ndTM6noUZT4XOf/8jSzD5C0MRJWeCrU+YjfPvmo1Wz29ny3/L5+XK7+OjjidnZN64IqgS"
    "HENKtbmm8GH6CnAZxzrluvUqu4eUVzbHkvx5DLwFVImsbXHvRG29B4PcU5+G0s06gwlGtaCdpgdk3"
    "KHQYAbhF1Uz5soZrLgGWQMgwQUqmwd8dibdqbUPMFzXsocmrhnFefotnUQiUdA6a+oJFMnY0i2kGh2"
    "naXg+Z723NLDxc7WeDpFuT1oizIpXihC9pzmd61jRtfVmjpHBTF9FtxW48BQbWb6680ETPYfRbVkbq"
    "hbg1PBwQ2jiCl2H9r7PSAFR1BcPpscupob4wfIAMMJCFMwJ/u3+5XNAD+5M/ThHLaDAXQCowM+wF+"
    "Ee6sCTjEmHY4ukYqGE/e4qvOsPWCPqdJl1wIwc3VAl4pQxZ7w7y1FdNs5XzEd8+lQDKoYtPG+13sK"
    "aiaUc9GvLBzPfVrUlFbHtQH/4+1TR0UadFhiteDKlS7hnjWphfOrTCET8AFYTKrrY9R8nZEyzFy5+s"
    "FtZyhW0EqAOV08ptBtg5svHu/S8oBWeJwwOFX+P31+enePP4zgzUF/44rJHViB2ffJvKFtik5O1HQ1"
    "2f6O21oMygsAGObkaKII03WFWfQ68GqDXQaj1cbqt+l2OAyiTrlWtom4Xw2Q8Ad1kvtAfgzJB8tY3j"
    "Qw1L/RBbkJQgeKA7dxi4NxuEtx7xCkznsSslShk5wpViZHW8HmBrntpswnDHkSXZRA2RqyTRB1DfKO"
    "vbGqAV1ePfLb3ZBDCoQ0Koe8/sUfIHFaflAadB+gPn/BX+ALGQ="
)

def _decode_humanizer() -> str:
    import zlib, base64
    return zlib.decompress(base64.b64decode(_HZR)).decode()


def _default_system_prompt(max_words: int, language: str) -> str:
    return textwrap.dedent(f"""\
        You are an expert cover letter writer. Your task is to produce a
        personalized, professional cover letter in Markdown format.

        Guidelines:
        - Language: {language}
        - Target length: {max_words} words (be concise and impactful)
        - Tone: confident, warm, genuine - never sycophantic
        - Start with a salutation (e.g. "Dear Hiring Team,")
        - Highlight 2-3 specific skills or experiences from the resume that
          directly match the job requirements. Use actual numbers, project
          names, and concrete outcomes from the resume - never vague summaries.
        - End with a professional closing and the applicant's name
        - Do NOT include a date, subject line, or postal addresses
        - Output ONLY the cover letter - no preamble, no commentary
        - Use ONLY plain ASCII characters. No em dashes, en dashes, smart
          quotes, curly apostrophes, ellipsis characters, or any Unicode
          punctuation. Use a plain hyphen (-) instead of any dash.
        - Do NOT add trailing spaces after sentences or at the end of lines.
        - No emojis or special symbols of any kind.

        Writing style (to sound natural and human):
        - Vary sentence length. Mix short sentences (under 8 words) with
          longer ones. Do not write every sentence at the same length.
        - Vary paragraph length. Not every paragraph should be the same size.
        - Use contractions naturally: I've, I'd, I'm, you'll, it's.
        - Write in active voice with strong verbs: built, shipped, cut, led,
          designed - not "was responsible for" or "helped to facilitate".
        - BANNED words and phrases - never use any of these:
          excited, thrilled, delighted, passionate, leverage, delve, foster,
          align with, showcase, demonstrate, furthermore, moreover, in
          conclusion, I am writing to express, I would be an excellent fit,
          I am eager to, synergy, dynamic, fast-paced, results-driven,
          detail-oriented, hard-working, team player, go-getter.
    """)


def _build_system_prompt() -> str:
    max_words = CFG["cover_letter"].get("max_words", 450)
    language  = CFG["cover_letter"].get("language", "English")
    custom    = CFG["cover_letter"].get("system_instructions", "").strip()
    if custom:
        return custom
    base = _default_system_prompt(max_words, language)
    # Humanizer: append anti-detection writing rules (local LLM only)
    if CFG["cover_letter"].get("humanize") and AI.provider in ("ollama", "llamacpp"):
        base += "\n\n" + _decode_humanizer()
    return base


def _build_user_prompt(req: GenerateRequest) -> str:
    parts = [
        f"## Role\n{req.job_title} at {req.company}",
        f"## Job Description\n{req.job_description.strip()}",
        f"## My Resume\n{req.resume_text.strip()}",
    ]

    profile = req.user_profile
    if profile:
        profile_lines = "\n".join(
            f"- {k}: {v}" for k, v in profile.items() if v
        )
        parts.append(f"## My Profile\n{profile_lines}")

    global_instr = req.global_instructions.strip() or CFG["cover_letter"].get("user_instructions", "").strip()
    if global_instr:
        parts.append(f"## Global Instructions (always apply)\n{global_instr}")

    if req.per_job_instructions.strip():
        parts.append(
            f"## Specific Instructions for This Application\n"
            f"{req.per_job_instructions.strip()}"
        )

    parts.append("Write the cover letter now:")
    return "\n\n".join(parts)


# ── HTML export ───────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Cover Letter — {role} at {company}</title>
  <style>
    body {{
      font-family: Georgia, 'Times New Roman', serif;
      max-width: 740px;
      margin: 60px auto;
      padding: 0 24px;
      color: #1a1a1a;
      line-height: 1.75;
      font-size: 16px;
    }}
    p {{ margin: 0 0 1em; }}
    strong {{ font-weight: 600; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""

def _md_to_html(cover_md: str, company: str, role: str) -> str:
    # Split off any trailing HTML comment (easter egg) before converting,
    # then re-append it so it stays outside the converted <p> tags.
    egg_comment = ""
    md_body = cover_md

    # Match the last HTML comment in the document — must be anchored at end.
    # Pattern avoids greedy matching across multiple comments by requiring no '-->'
    # inside the comment body (standard HTML comment constraint).
    comment_match = re.search(r"\n(<!--(?:[^-]|-(?!->))*-->)\s*$", cover_md)
    if comment_match:
        egg_comment = "\n" + comment_match.group(1)
        md_body = cover_md[: comment_match.start()]

    body_html = md_lib.markdown(md_body)
    full_body = body_html + egg_comment

    return _HTML_TEMPLATE.format(
        company=_html.escape(company),
        role=_html.escape(role),
        body=full_body,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_output(text: str) -> str:
    """Strip trailing whitespace from every line and replace non-ASCII punctuation."""
    # Common non-ASCII punctuation replacements
    replacements = [
        ('—', '-'),   # em dash
        ('–', '-'),   # en dash
        ('‒', '-'),   # figure dash
        ('‘', "'"),   # left single quotation mark
        ('’', "'"),   # right single quotation mark / apostrophe
        ('“', '"'),   # left double quotation mark
        ('”', '"'),   # right double quotation mark
        ('…', '...'), # horizontal ellipsis
        (' ', ' '),   # non-breaking space
    ]
    for src, dst in replacements:
        text = text.replace(src, dst)
    # Strip trailing whitespace from every line (catches "sentence.  " patterns)
    lines = [line.rstrip() for line in text.splitlines()]
    return '\n'.join(lines).strip()


def _unwrap_fences(text: str) -> str:
    """Remove ```markdown … ``` wrappers that some models add."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (``` or ```markdown) and last ``` line
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text


def _safe_name(name: str) -> str:
    """Convert a company/role/model name to a safe directory component."""
    name = name.strip()
    name = re.sub(r"[:.]", "-", name)          # colon/dot → hyphen (e.g. qwen3:8b, gpt-4.1-mini)
    name = re.sub(r"[^\w\s-]", "", name)       # remove remaining special chars
    name = re.sub(r"[\s_]+", "-", name)        # spaces → hyphens
    name = re.sub(r"-{2,}", "-", name)         # collapse multiple hyphens
    name = name.strip("-")
    return name[:64] or "unknown"              # cap length


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="grapply-companion")
    parser.add_argument("--port",      type=int, default=CFG.get("companion_port", 7878))
    parser.add_argument("--host",      default="127.0.0.1")
    parser.add_argument("--reload",    action="store_true", help="Dev mode auto-reload")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"],
                        help="Log verbosity (default: info)")
    parser.add_argument("--provider",  default=None,
                        choices=["ollama", "openai", "claude"],
                        help="Override AI provider from config.toml")
    parser.add_argument("--model",     default=None,
                        help="Override AI model from config.toml")
    parser.add_argument("--endpoint",  default=None,
                        help="Override AI endpoint URL from config.toml")
    parser.add_argument("--api-key",   default=None, dest="api_key",
                        help="Override AI API key from config.toml")
    args = parser.parse_args()

    # Apply log level to all grapply loggers
    level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)

    # Apply CLI overrides to the global AI client and CFG
    global AI
    if any([args.provider, args.model, args.endpoint, args.api_key]):
        override = dict(CFG["ai"])
        if args.provider:  override["provider"] = args.provider
        if args.model:     override["model"]    = args.model
        if args.endpoint:  override["endpoint"] = args.endpoint.rstrip("/")
        if args.api_key:   override["api_key"]  = args.api_key
        CFG["ai"] = override
        AI = ai_module.AIClient(override)

    print(f"\ngrapply companion  |  http://{args.host}:{args.port}")
    print(f"  Config       : {cfg_module._config_path()}")
    print(f"  AI provider  : {AI.provider}  ({AI.endpoint})")
    print(f"  AI model     : {AI.model}")
    print(f"  Output dir   : {CFG['output_dir']}")
    print(f"  Log level    : {args.log_level}")

    from tool_server import list_parsers
    cached = list_parsers()
    if cached["count"]:
        print(f"  Cached parsers ({cached['count']}):", ", ".join(p["domain"] for p in cached["parsers"]))
    else:
        print("  Cached parsers: none (will generate on first scan of each site)")

    warnings = cfg_module.get_setup_warnings(CFG)
    if warnings:
        print()
        for w in warnings:
            print(f"  ⚠  {w}")

    # Always cache the extracted resume text so the user can inspect it
    cfg_module.load_resume_text(CFG)

    # Auto-populate profile from resume on first run
    if cfg_module.profile_needs_population(CFG):
        _populate_profile_from_resume()

    print()

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        log_config=None,  # don't let uvicorn override our logging setup
    )


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════════════════════════
# Job discovery - the other half of grapply: find postings worth grabbing.
# CLI equivalent:  python -m companion.discovery --rank
# ══════════════════════════════════════════════════════════════════════════════

_DISCOVERY_STATE: dict = {"running": False, "stage": "idle", "done": 0,
                          "total": 0, "error": "", "finished": ""}


def _discovery_worker(prefilter_min: float | None, limit: int | None,
                      do_llm: bool, do_rank: bool, include_seen: bool,
                      since_days: int | None = None,
                      market: str | None = None) -> None:
    import discovery
    def progress(stage: str, done: int, total: int) -> None:
        _DISCOVERY_STATE.update(stage=stage, done=done, total=total)
    try:
        _DISCOVERY_STATE.update(running=True, stage="starting", done=0,
                                total=0, error="", finished="")
        discovery.run_scan(prefilter_min=prefilter_min, limit=limit,
                           do_llm=do_llm, do_rank=do_rank,
                           include_seen=include_seen, since_days=since_days,
                           market=market, progress=progress)
    except Exception as exc:                                    # noqa: BLE001
        logger.exception("[discovery] scan failed")
        _DISCOVERY_STATE.update(error=str(exc), stage="error")
    finally:
        _DISCOVERY_STATE.update(
            running=False,
            finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))


@app.get("/discovery", response_class=HTMLResponse)
def discovery_page():
    token = CFG.get("auth_token", "")
    return HTMLResponse(content=_DISCOVERY_HTML.replace("%%AUTH_TOKEN%%", token))


@app.get("/discovery/results")
def discovery_results_endpoint():
    import discovery
    return discovery.load_last()


@app.get("/discovery/status")
def discovery_status_endpoint():
    return dict(_DISCOVERY_STATE)


@app.get("/discovery/sources")
def discovery_sources_endpoint():
    import discovery
    return {"sources": discovery.load_sources(),
            "path": str(discovery.SOURCES_PATH)}


@app.get("/discovery/config")
def discovery_config_endpoint():
    """Search phases and thresholds, so they can be edited from the UI rather
    than by hand-editing JSON."""
    import discovery
    cfg = discovery.load_config()
    try:
        import render
        engines = render.usable_engines()
        browser = render.available()
        profiles = sorted(render.SITE_PROFILES)
    except Exception:                                           # noqa: BLE001
        engines, browser, profiles = [], False, []
    markets = {}
    for name in sorted(set(discovery.MARKETS) | set(cfg.get("markets") or {})):
        try:
            markets[name] = discovery.resolve_market(name, cfg)["label"]
        except ValueError:
            continue
    return {"config": cfg, "path": str(discovery.CONFIG_PATH),
            "markets": markets,
            "browser": {"available": browser, "engines": engines,
                        "profiles": profiles}}


@app.post("/discovery/config")
def discovery_config_save_endpoint(payload: dict,
                                   _: None = Depends(_require_token)):
    import discovery
    cfg = payload.get("config", payload)
    if not isinstance(cfg, dict) or not cfg.get("phases"):
        raise HTTPException(status_code=400, detail="config needs 'phases'")
    discovery.save_config(cfg)
    return {"saved": True, "path": str(discovery.CONFIG_PATH)}


@app.post("/discovery/dismiss")
def discovery_dismiss_endpoint(payload: dict,
                               _: None = Depends(_require_token)):
    """Hide a posting for good. This is the only thing that removes a role from
    future scans - being seen no longer does."""
    import discovery
    job_id = (payload or {}).get("id", "")
    if not job_id:
        raise HTTPException(status_code=400, detail="id required")
    if (payload or {}).get("applied"):
        discovery.mark_applied(job_id)
    else:
        discovery.dismiss(job_id)
    return {"ok": True, "id": job_id}


@app.post("/discovery/run")
def discovery_run_endpoint(
    prefilter_min: float | None = None,
    limit: int | None = None,
    llm: bool = True,
    rank: bool = True,
    include_seen: bool = True,
    since_days: int | None = None,
    market: str | None = None,
    _: None = Depends(_require_token),
):
    if _DISCOVERY_STATE["running"]:
        raise HTTPException(status_code=409, detail="A scan is already running")
    if market:
        import discovery
        try:
            discovery.resolve_market(market, discovery.load_config())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    import threading
    threading.Thread(
        target=_discovery_worker,
        args=(prefilter_min, limit, llm, rank, include_seen, since_days,
              market or None),
        daemon=True,
    ).start()
    return {"started": True}


_DISCOVERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>grapply - Discovery</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    :root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;
          --ok:#4caf7d;--warn:#d8a657;--danger:#e05252;--text:#e8eaf0;--muted:#7b7f96}
    body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh}
    header{padding:20px 32px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
    .logo{font-weight:700;font-size:20px;letter-spacing:-.3px}.logo span{color:var(--accent)}
    .subtitle{color:var(--muted);font-size:13px}
    .spacer{flex:1}
    main{max-width:1180px;margin:0 auto;padding:28px 24px 60px}
    section{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px 22px;margin-bottom:20px}
    h2{font-size:13px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--border)}
    button{background:var(--accent);border:0;border-radius:6px;color:#fff;cursor:pointer;font-size:13px;font-weight:600;padding:9px 16px;font-family:inherit}
    button:disabled{opacity:.45;cursor:not-allowed}
    button.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
    .controls{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}
    .fld label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}
    .fld input,.fld select{background:#0f1117;border:1px solid var(--border);border-radius:6px;color:var(--text);padding:7px 9px;font-size:13px;font-family:inherit;width:120px}
    .chk{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);padding-bottom:8px}
    .hint{font-size:11px;color:var(--muted);line-height:1.45;margin-top:3px;max-width:190px}
    .opt{display:flex;flex-direction:column;gap:1px;margin-bottom:9px}
    .opt .chk{padding-bottom:0}
    .opt .hint{max-width:none;margin-left:22px}
    .warn{color:var(--warn)}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
    .card{background:#0f1117;border:1px solid var(--border);border-radius:8px;padding:14px 16px}
    .card .n{font-size:24px;font-weight:700;letter-spacing:-.5px}
    .card .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
    td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top}
    tr:last-child td{border-bottom:0}
    tr.skip{opacity:.5}
    a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
    .rank{font-weight:700;color:var(--muted);width:34px}
    .pill{display:inline-block;font-size:11px;font-weight:600;padding:3px 8px;border-radius:20px;white-space:nowrap}
    .v-strong{background:rgba(76,175,125,.16);color:var(--ok)}
    .v-worth{background:rgba(108,99,255,.18);color:#9b95ff}
    .v-marginal{background:rgba(216,166,87,.16);color:var(--warn)}
    .v-skip{background:rgba(224,82,82,.14);color:var(--danger)}
    .why{color:var(--muted);font-size:12px;max-width:340px}
    .meta{color:var(--muted);font-size:12px}
    .note{color:var(--warn);font-size:11px;margin-top:4px}
    .bar{height:5px;background:#0f1117;border-radius:3px;overflow:hidden;margin-top:12px}
    .bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
    .empty{color:var(--muted);text-align:center;padding:34px 0}
    .hits{color:var(--muted);font-size:11px;margin-top:4px}
  </style>
</head>
<body>
<header>
  <div class="logo">gr<span>apply</span></div>
  <div class="subtitle">Discovery</div>
  <div class="spacer"></div>
  <a class="meta" href="/stats">stats</a>
  <a class="meta" href="/settings">settings</a>
</header>
<main>
  <section>
    <h2>Scan</h2>
    <div class="meta" style="margin-bottom:14px">
      Searches job boards, throws away what does not fit your CV, and hands you
      a short list. It only reads and ranks - it never applies for anything.
    </div>
    <div class="controls">
      <div class="fld">
        <label for="pf">How picky? (0-100)</label>
        <input id="pf" type="number" value="55" step="5" min="0" max="100">
        <div class="hint">A job must score at least this on keyword matching
          to survive. Lower finds more jobs but more rubbish. 45-55 is sensible.</div>
      </div>
      <div class="fld">
        <label for="lim">How many to read closely</label>
        <input id="lim" type="number" value="25" min="1" max="100">
        <div class="hint">Only the best this-many get read by the AI, because
          that part is slow. 25 takes a couple of minutes.</div>
      </div>
      <div class="fld">
        <label for="market">Where to look</label>
        <select id="market">
          <option value="" selected>As configured (all enabled phases)</option>
        </select>
        <div class="hint">Pick a country to run a one-off scan of just that
          market - the company pages, the right job boards, and a location
          filter for someone based there. Does not change your saved phases.</div>
      </div>
      <div class="fld">
        <label for="since">Posted within</label>
        <select id="since">
          <option value="0" selected>Any time</option>
          <option value="1">Last 24 hours</option>
          <option value="3">Last 3 days</option>
          <option value="7">Last week</option>
          <option value="14">Last 2 weeks</option>
        </select>
        <div class="hint">"Last 24 hours" shows only what appeared since
          yesterday, which is what you want on a rescan. Postings whose date a
          board does not publish are kept rather than guessed at.</div>
      </div>
      <div style="min-width:280px">
        <div class="opt">
          <label class="chk" for="llm"><input id="llm" type="checkbox" checked>
            Have the AI read each job</label>
          <div class="hint">Scores fit out of 10 and names your gaps.</div>
        </div>
        <div class="opt">
          <label class="chk" for="rank"><input id="rank" type="checkbox" checked>
            Have the AI put them in order</label>
          <div class="hint warn">Needs a big model. On the 8B one you are
            running it grades everything "strong" and skips numbers - leave
            this off until you point it at something larger.</div>
        </div>
        <div class="opt">
          <label class="chk" for="seen"><input id="seen" type="checkbox" checked>
            Include jobs I have already seen</label>
          <div class="hint">Off means only brand-new postings. A job you have
            not applied to is still worth seeing, so this is normally on.</div>
        </div>
      </div>
      <div style="display:flex;gap:10px;align-items:center">
        <button id="run">Run scan</button>
        <button id="refresh" class="ghost">Refresh</button>
      </div>
    </div>
    <div class="bar"><i id="bar"></i></div>
    <div class="meta" id="status" style="margin-top:8px">idle</div>
  </section>

  <section>
    <h2>What to search for</h2>
    <div class="meta" style="margin-bottom:10px">
      Type the job titles you want, one per line - the same words you would
      type into a job site. These are used on Seek and Indeed. The company
      career pages and the remote job feeds are read in full instead, so these
      words do not limit those.
      <br>Each line is a separate search, so more lines means a longer scan.
    </div>
    <div id="phases"></div>
    <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
      <button id="savekw">Save keywords</button>
      <button id="resetkw" class="ghost">Reload</button>
      <span class="meta" id="kwstatus"></span>
    </div>
  </section>

  <section>
    <h2>Last scan</h2>
    <div class="cards" id="cards"></div>
    <div class="meta" id="generated" style="margin-top:12px"></div>
  </section>

  <section>
    <h2>Shortlist - your call</h2>
    <div id="tablewrap"><div class="empty">No results yet. Run a scan.</div></div>
  </section>
</main>
<script>
const TOKEN = '%%AUTH_TOKEN%%';
const H = TOKEN ? {'X-Grapply-Token': TOKEN} : {};
const $ = id => document.getElementById(id);

function pill(v){
  const k = (v||'').toLowerCase();
  const cls = k.startsWith('strong') ? 'v-strong'
            : k.startsWith('worth')  ? 'v-worth'
            : k.startsWith('marg')   ? 'v-marginal'
            : k.startsWith('skip')   ? 'v-skip' : 'v-worth';
  return v ? `<span class="pill ${cls}">${v}</span>` : '';
}

function render(d){
  const c = d.counts || {}, jobs = d.jobs || [];
  $('cards').innerHTML = [
    ['Jobs looked at', c.fetched ?? '-',
     'Every posting pulled from every site'],
    ['Did not fit', c.rejected ?? '-',
     'Wrong language, wrong place, or not a software job'],
    ['Worth a look', c.survivors ?? '-',
     'Passed the checks against your CV'],
    ['On your list', c.shortlist ?? jobs.length,
     'The best of those, shown below'],
  ].map(([l,n,h]) => `<div class="card" title="${h}"><div class="n">${n}</div>` +
                     `<div class="l">${l}</div>` +
                     `<div class="hint" style="margin-top:5px">${h}</div></div>`).join('');
  $('generated').textContent = d.generated
    ? 'Last run ' + new Date(d.generated).toLocaleString() : '';

  if (!jobs.length){
    $('tablewrap').innerHTML = '<div class="empty">No results yet. Run a scan.</div>';
    return;
  }
  jobs.sort((a,b) => (a.rank ?? 999) - (b.rank ?? 999)
                  || (b.llm_score ?? 0) - (a.llm_score ?? 0));
  const rows = jobs.map(j => {
    const hits = Object.entries(j.hits || {})
      .map(([k,v]) => k + ': ' + v.slice(0,4).join(', ')).join(' | ');
    const notes = (j.notes || []).map(n => `<div class="note">${n}</div>`).join('');
    const skip = (j.verdict||'').toLowerCase().startsWith('skip') ? ' class="skip"' : '';
    return `<tr${skip}>
      <td class="rank">${j.rank ?? ''}</td>
      <td><a href="${j.url}" target="_blank" rel="noopener">${j.title}</a>
          <div class="hits">${hits}</div>${notes}</td>
      <td>${j.company}</td>
      <td class="meta">${j.location || ''}</td>
      <td>${(j.llm_score ?? '-')}<span class="meta">/10</span>
          <div class="meta">pf ${j.prefilter_score ?? '-'}</div></td>
      <td>${pill(j.verdict)}</td>
      <td class="why">${j.why || (j.llm && j.llm.recommendation) || ''}</td>
    </tr>`;
  }).join('');
  $('tablewrap').innerHTML = `<table><thead><tr>
      <th>#</th><th>Role</th><th>Company</th><th>Location</th>
      <th>Fit</th><th>Verdict</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function loadResults(){
  const d = await fetch('/discovery/results', {headers:H}).then(r=>r.json());
  render(d);
}

let poll = null;
async function tick(){
  const s = await fetch('/discovery/status', {headers:H}).then(r=>r.json());
  const pct = s.total ? Math.round(100 * s.done / s.total) : (s.running ? 5 : 0);
  $('bar').style.width = pct + '%';
  $('status').textContent = s.error ? ('error: ' + s.error)
    : s.running ? `${s.stage} ${s.done}/${s.total}` : 'idle';
  $('run').disabled = s.running;
  if (!s.running && poll){ clearInterval(poll); poll = null; loadResults(); }
}

$('run').onclick = async () => {
  const q = new URLSearchParams({
    prefilter_min: $('pf').value, limit: $('lim').value,
    llm: $('llm').checked, rank: $('rank').checked,
    include_seen: $('seen').checked, since_days: $('since').value });
  if ($('market').value) q.set('market', $('market').value);
  const r = await fetch('/discovery/run?' + q, {method:'POST', headers:H});
  if (!r.ok){ $('status').textContent = 'error: ' + (await r.text()); return; }
  $('run').disabled = true;
  if (!poll) poll = setInterval(tick, 1500);
  tick();
};
$('refresh').onclick = loadResults;

// ── keyword editor ──────────────────────────────────────────────────────────
// Phases are edited in place and written back whole, so anything the UI does
// not expose (gates, source lists, limits) survives a save untouched.
let CFG = null;

function renderPhases(){
  if (!CFG) return;
  $('phases').innerHTML = CFG.phases.map((p, i) => `
    <div style="margin-bottom:14px">
      <label class="chk" style="padding:0 0 6px">
        <input type="checkbox" data-en="${i}" ${p.enabled !== false ? 'checked' : ''}>
        <b style="color:var(--text)">${p.name}</b>
        <span class="meta">&nbsp;${
          (m => m === 'nz' ? 'keeps only jobs you could do from New Zealand'
             : m === 'remote' ? 'keeps only fully remote jobs'
             : m ? ('location filter: ' + m)
             : 'keeps everything')(p.market || p.gate)}${
          (p.browser_profiles||[]).length
            ? ' · opens a browser for ' + p.browser_profiles
                .map(s => s.replace('-nz','').replace(/^./, m => m.toUpperCase()))
                .join(', ')
            : ''}</span>
      </label>
      <textarea data-kw="${i}" rows="${Math.max(3,(p.keywords||[]).length+1)}"
        style="width:100%;background:#0f1117;border:1px solid var(--border);
               border-radius:6px;color:var(--text);padding:8px 10px;
               font-size:13px;font-family:inherit;resize:vertical"
        >${(p.keywords||[]).join('\\n')}</textarea>
    </div>`).join('');
}

async function loadConfig(){
  try{
    const d = await fetch('/discovery/config', {headers:H}).then(r=>r.json());
    CFG = d.config; renderPhases();
    const sel = $('market');
    if (d.markets && sel.options.length <= 1){
      for (const [k, label] of Object.entries(d.markets)){
        const o = document.createElement('option');
        o.value = k; o.textContent = label;
        sel.appendChild(o);
      }
    }
    $('kwstatus').textContent = d.browser && d.browser.available
      ? '' : 'browser sources unavailable (playwright not installed)';
  }catch(e){ $('kwstatus').textContent = 'could not load config'; }
}

$('savekw').onclick = async () => {
  if (!CFG) return;
  document.querySelectorAll('[data-kw]').forEach(t => {
    const i = +t.dataset.kw;
    CFG.phases[i].keywords = t.value.split('\\n')
      .map(s => s.trim()).filter(Boolean);
  });
  document.querySelectorAll('[data-en]').forEach(c => {
    CFG.phases[+c.dataset.en].enabled = c.checked;
  });
  $('kwstatus').textContent = 'saving...';
  const r = await fetch('/discovery/config',
    {method:'POST', headers:{...H,'Content-Type':'application/json'},
     body: JSON.stringify({config: CFG})});
  $('kwstatus').textContent = r.ok
    ? 'saved - applies to the next scan'
    : 'save failed: ' + (await r.text()).slice(0,90);
};
$('resetkw').onclick = loadConfig;

loadResults(); tick(); loadConfig();
</script>
</body>
</html>"""
