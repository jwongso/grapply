"""
grapply - browser rendering layer

Most job sources serve JSON and need nothing from this module. Some do not:
single-page apps render postings client-side, and a few boards reject plain
HTTP clients outright. This is the fallback for those.

The technique that matters here is XHR capture, not HTML parsing. A SPA has to
call its own backend to draw a results page, so we drive the page and record the
responses it fetches. That yields the site's own structured JSON, and it keeps
working when the markup is restyled or the bundle is obfuscated - Zeil ships a
minified bundle with no recoverable endpoint strings, yet still has to make the
call at runtime.

Sites are described declaratively in SITE_PROFILES: a start URL, a pattern for
the interesting responses, and a field mapping. Adding a board is a data change,
not a new scraper. One engine drives all of them.

Before adding a profile, check the site's terms of service, and prefer a public
API where one exists. A Trade Me profile was removed for this reason: its data
sat behind an authenticated endpoint, working around that is not something to
automate, and Seek's public API already returns far more New Zealand postings.
A profile that has to defeat an access control is the signal to stop, not a
problem to solve.

Playwright is an optional dependency. If it is missing, available() reports
False and callers fall back to the JSON sources rather than failing the scan.

    python -m companion.render --demo seek-nz --keywords "c++"   # headed
    python -m companion.render --list
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# Selectors that were proven to work, remembered between runs. Job boards
# reshuffle their markup and ship obfuscated class names, so a profile lists
# several candidates per field and the first one that actually yields data is
# cached here. A later run tries the remembered selector first and only falls
# back to probing when it stops matching - the profile heals itself instead of
# needing a code change every time a site is restyled.
LEARNED_PATH = Path("~/.grapply/learned_selectors.json").expanduser()

# Browser profile directories, one per engine/device. These hold whatever the
# browser itself stores - cookies, local storage - so a sign-in done by hand
# once carries into later scans.
PROFILE_ROOT = Path("~/.grapply/browser").expanduser()

try:                                                    # optional dependency
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_ERR = ""
except ImportError as exc:                              # pragma: no cover
    sync_playwright = None                              # type: ignore
    _PLAYWRIGHT_ERR = str(exc)


def available() -> bool:
    """True when browser-backed sources can run."""
    return sync_playwright is not None


def why_unavailable() -> str:
    if available():
        return ""
    return (f"playwright not installed ({_PLAYWRIGHT_ERR}). "
            "pip install playwright && playwright install chromium")


# Playwright ships three independent engines, so rotating between them is free
# diversity: each presents its own TLS fingerprint, JS engine and viewport
# rather than every request looking like the same headless Chrome. Rotation is
# per site, because the engine is fixed at browser launch.
_LOCALE = "en-NZ"
_TZ = "Pacific/Auckland"

ENGINES = ("chromium", "firefox", "webkit")
DEVICES = ("desktop", "mobile")

_ENGINE_ARGS: dict[str, list[str]] = {
    "chromium": ["--disable-blink-features=AutomationControlled",
                 "--no-sandbox"],
    "firefox": [],
    "webkit": [],
}

# Desktop and mobile are worth trying separately, not just cosmetically: many
# boards serve a lighter template and a different API to phones, and a lazy
# desktop grid often becomes a plain paginated list on mobile. Which one yields
# more is a per-site empirical question - see compare_devices().
#
# Viewports stay consistent with the engine so the profile is not self-
# contradictory (Safari reporting a Chrome window, say). Firefox has no
# is_mobile/has_touch support in Playwright, so its mobile profile is a narrow
# viewport plus a mobile UA and nothing that would throw.
_DEVICE_PROFILE: dict[tuple[str, str], dict] = {
    ("chromium", "desktop"): {"viewport": {"width": 1440, "height": 900}},
    ("firefox",  "desktop"): {"viewport": {"width": 1366, "height": 768}},
    ("webkit",   "desktop"): {"viewport": {"width": 1512, "height": 945}},
    ("chromium", "mobile"): {
        "viewport": {"width": 393, "height": 851}, "is_mobile": True,
        "has_touch": True, "device_scale_factor": 3,
        "user_agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Mobile Safari/537.36")},
    ("webkit", "mobile"): {
        "viewport": {"width": 390, "height": 844}, "is_mobile": True,
        "has_touch": True, "device_scale_factor": 3,
        "user_agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.5 Mobile/15E148 Safari/604.1")},
    ("firefox", "mobile"): {
        "viewport": {"width": 412, "height": 915},
        "user_agent": ("Mozilla/5.0 (Android 14; Mobile; rv:128.0) "
                       "Gecko/128.0 Firefox/128.0")},
}


def _context_options(engine: str, device: str) -> dict:
    opts = dict(_DEVICE_PROFILE[(engine, device)])
    opts.update(locale=_LOCALE, timezone_id=_TZ, java_script_enabled=True)
    return opts


_usable: list[str] | None = None


def usable_engines(refresh: bool = False) -> list[str]:
    """Engines that actually launch on this host, probed once and cached.

    An installed engine is not a working one: WebKit in particular needs system
    libraries that Playwright cannot install itself outside Debian-family
    distros. Rotation has to skip those rather than fail a site.
    """
    global _usable
    if _usable is not None and not refresh:
        return _usable
    if not available():
        _usable = []
        return _usable
    ok: list[str] = []
    with sync_playwright() as pw:
        for eng in ENGINES:
            try:
                b = getattr(pw, eng).launch(headless=True)
                b.close()
                ok.append(eng)
            except Exception:                                    # noqa: BLE001
                pass
    _usable = ok or ["chromium"]
    return _usable


def _resolve_engine(engine: str) -> str:
    """'random' picks per call; anything else is taken literally."""
    if engine in ENGINES:
        return engine
    return random.choice(usable_engines())


def engine_cycle(seed: int | None = None) -> "Callable[[], str]":
    """Shuffled round-robin over the working engines.

    Better than independent random draws for a multi-site scan: it spreads the
    load evenly instead of occasionally sending everything through one engine.
    """
    rng = random.Random(seed)
    pool = usable_engines()
    order: list[str] = []

    def nxt() -> str:
        nonlocal order
        if not order:
            order = list(pool)
            rng.shuffle(order)
        return order.pop()

    return nxt


class BrowserSession:
    """Thin wrapper over a Playwright context.

    Usable as a context manager. Keep one session across several fetches - the
    browser launch dominates the cost of a small scrape.
    """

    def __init__(self, headless: bool = True, slow_mo: int = 0,
                 timeout: float = 45.0, engine: str = "chromium",
                 device: str = "desktop", persist: bool = True) -> None:
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout * 1000
        self.engine = _resolve_engine(engine)
        self.device = device if device in DEVICES else "desktop"
        # Persist the cookie jar so a manual sign-in survives between runs.
        # Signed-in sessions see more listings and are treated less like a bot.
        # Credentials are typed by the user into the visible browser; nothing
        # here reads, stores or transmits them - only the profile directory
        # Chromium/Firefox writes for itself.
        self.persist = persist
        self._pw = None
        self._browser = None
        self._ctx = None

    def profile_dir(self) -> Path:
        p = PROFILE_ROOT / f"{self.engine}-{self.device}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        if not available():
            raise RuntimeError(why_unavailable())
        self._pw = sync_playwright().start()
        launcher = getattr(self._pw, self.engine)
        # Only chromium accepts the flag list; the others reject unknown args.
        kw: dict = {"headless": self.headless, "slow_mo": self.slow_mo}
        if _ENGINE_ARGS[self.engine]:
            kw["args"] = _ENGINE_ARGS[self.engine]

        if self.persist:
            # A persistent context IS the context - there is no separate
            # browser object to create pages from.
            try:
                self._ctx = launcher.launch_persistent_context(
                    str(self.profile_dir()), **kw,
                    **_context_options(self.engine, self.device))
            except Exception as exc:                             # noqa: BLE001
                # A profile can only be opened once. Hitting this normally
                # means a --login window is still up, which is easy to fix and
                # deserves a sentence rather than a Playwright stack trace.
                if "already in use" in str(exc) or "existing browser" in str(exc):
                    raise RuntimeError(
                        f"browser profile {self.profile_dir()} is already "
                        "open - close the other window (often a leftover "
                        "--login session), or pass persist=False to use a "
                        "throwaway profile") from None
                raise
        else:
            self._browser = launcher.launch(**kw)
            self._ctx = self._browser.new_context(
                **_context_options(self.engine, self.device))
        self._ctx.set_default_timeout(self.timeout)

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:                                    # noqa: BLE001
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:                                        # noqa: BLE001
            pass
        self._pw = self._browser = self._ctx = None

    # ── primitives ───────────────────────────────────────────────────────────

    def fetch_html(self, url: str, *, wait_for: str = "",
                   scroll_passes: int = 0) -> str:
        """Render a page and return its DOM. Use when a site has no usable XHR
        and the markup itself carries the data."""
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=15000)
                except Exception:                                # noqa: BLE001
                    pass
            self._scroll(page, scroll_passes)
            return page.content()
        finally:
            page.close()

    # Consent walls block the XHRs we came for, and every vendor uses its own
    # markup, so try the common ones and move on if none are present.
    _CONSENT = (
        "#onetrust-accept-btn-handler",
        "button[aria-label*='Accept' i]",
        "button[data-testid*='accept' i]",
        "button[id*='accept' i]",
        "button[class*='accept' i]",
        "[data-cookie-banner] button",
    )

    def dismiss_consent(self, page: Any) -> bool:
        for sel in self._CONSENT:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    self._pace()
                    return True
            except Exception:                                    # noqa: BLE001
                continue
        return False

    def prime(self, page: Any, url: str) -> None:
        """Land on the site normally before deep-linking into a search.

        A cold request straight to a results URL has no cookies, no session and
        no referer, which is the least browser-like thing a browser can do.
        Visiting the entry page first is both more reliable and closer to what
        an actual visit looks like.
        """
        try:
            page.goto(url, wait_until="domcontentloaded")
            self.dismiss_consent(page)
            self._pace()
            page.mouse.move(random.randint(200, 900), random.randint(150, 600))
            self._pace()
        except Exception:                                        # noqa: BLE001
            pass

    def screenshot(self, page: Any, path: str, full: bool = False) -> str:
        """Capture what the page currently looks like.

        Worth reaching for whenever a selector "should" work and does not: a
        consent wall, an interstitial or an empty result set are all obvious in
        an image and invisible in a query_selector that simply returned None.
        """
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=path, full_page=full)
            return path
        except Exception:                                        # noqa: BLE001
            return ""

    def search_in_page(self, page: Any, selector: str, text: str) -> bool:
        """Type a query into the site's own search box and submit.

        This is not decoration. Some SPAs server-render a directly requested
        results URL and only call their search API on client-side navigation -
        Trade Me does exactly that, so deep-linking yields nothing to capture
        while typing the same query yields the JSON. Driving the site's own
        controls is both more reliable and more faithful than URL guessing.
        """
        try:
            box = page.wait_for_selector(selector, timeout=10000)
            if not box:
                return False
            box.click()
            self._pace()
            # Per-character typing so the SPA's input handlers actually fire.
            box.type(text, delay=random.randint(60, 130))
            self._pace()
            box.press("Enter")
            return True
        except Exception:                                        # noqa: BLE001
            return False

    def harvest_dom(self, url: str, site: str, spec: dict, learned: dict, *,
                    max_pages: int = 5, max_jobs: int = 100,
                    warmup: str = "", search_selector: str = "",
                    search_text: str = "", wait_for: str = "",
                    scroll_passes: int = 0, settle: float = 2.0,
                    next_selectors: Iterable[str] = (),
                    on_page: Callable[[int, int, int], None] | None = None,
                    ) -> list[dict]:
        """Land once, then walk the result pages by clicking through them.

        Navigating page 1, extracting, finding the next control and clicking it
        keeps one session and one scroll position, exactly as a person reading
        listings would. Rebuilding a URL per page throws that state away, and on
        sites that paginate client-side there is no URL to rebuild. Each page is
        accumulated incrementally, so a run that breaks at page 4 still returns
        pages 1 to 3.
        """
        page = self._ctx.new_page()
        rows: list[dict] = []
        seen: set[str] = set()
        mem = learned.setdefault(site, {})
        nexts = list(next_selectors) or [
            "a[data-testid='pagination-page-next']",
            "a[aria-label='Next Page']", "a[aria-label='Next']",
            "button[aria-label='Next']", "a[rel=next]",
            "[data-automation='page-next']",
        ]
        try:
            if warmup:
                self.prime(page, warmup)
            if search_selector and search_text:
                if not self.search_in_page(page, search_selector, search_text):
                    page.goto(url, wait_until="domcontentloaded")
            else:
                page.goto(url, wait_until="domcontentloaded")
            self.dismiss_consent(page)

            for page_no in range(1, max_pages + 1):
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=15000)
                    except Exception:                            # noqa: BLE001
                        pass
                self._scroll(page, scroll_passes)
                time.sleep(settle)

                got = extract_dom(page, site, spec, learned)
                fresh = 0
                for rec in got:
                    ext = str(rec.get("ext_id") or "")
                    if not ext or ext in seen:
                        continue
                    seen.add(ext)
                    rows.append(rec)
                    fresh += 1
                    if len(rows) >= max_jobs:
                        break
                if on_page:
                    on_page(page_no, fresh, len(rows))
                if len(rows) >= max_jobs or fresh == 0:
                    break

                # Advance from where we are rather than reconstructing a URL.
                clicked = False
                ordered = ([mem.get("next", "")] + nexts
                           if mem.get("next") else nexts)
                for sel in ordered:
                    if not sel:
                        continue
                    try:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.scroll_into_view_if_needed(timeout=3000)
                            self._pace()
                            btn.click(timeout=5000)
                            mem["next"] = sel
                            clicked = True
                            break
                    except Exception:                            # noqa: BLE001
                        continue
                if not clicked:
                    break
                self._pace()
            return rows
        finally:
            page.close()

    def capture_json(self, url: str, pattern: str, *, wait_for: str = "",
                     scroll_passes: int = 0, next_selector: str = "",
                     max_pages: int = 1, settle: float = 2.0,
                     warmup: str = "", search_selector: str = "",
                     search_text: str = "") -> list[Any]:
        """Drive a page and collect every JSON response whose URL matches.

        This is the general answer to SPAs: the page fetches its own data, we
        record it. Returns raw decoded payloads in arrival order.
        """
        rx = re.compile(pattern, re.I)
        payloads: list[Any] = []
        page = self._ctx.new_page()

        def on_response(resp: Any) -> None:
            if not rx.search(resp.url):
                return
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            try:
                payloads.append(resp.json())
            except Exception:                                    # noqa: BLE001
                pass

        if warmup:
            # Prime before attaching the listener so warmup traffic is not
            # mistaken for results.
            self.prime(page, warmup)
        page.on("response", on_response)
        try:
            if search_selector and search_text:
                # Already primed on the entry page; search from there so the
                # SPA routes client-side and calls its API.
                if not self.search_in_page(page, search_selector, search_text):
                    page.goto(url, wait_until="domcontentloaded")
            else:
                page.goto(url, wait_until="domcontentloaded")
            self.dismiss_consent(page)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=15000)
                except Exception:                                # noqa: BLE001
                    pass
            self._scroll(page, scroll_passes)
            time.sleep(settle)

            for _ in range(max(0, max_pages - 1)):
                if not next_selector:
                    break
                try:
                    btn = page.query_selector(next_selector)
                    if not btn:
                        break
                    btn.click()
                except Exception:                                # noqa: BLE001
                    break
                self._pace()
                self._scroll(page, scroll_passes)
                time.sleep(settle)
            return payloads
        finally:
            page.remove_listener("response", on_response)
            page.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _scroll(self, page: Any, passes: int) -> None:
        """Lazy-loaded result lists only render what has been scrolled past."""
        for _ in range(passes):
            try:
                page.mouse.wheel(0, random.randint(1200, 2000))
            except Exception:                                    # noqa: BLE001
                return
            self._pace()

    @staticmethod
    def _pace() -> None:
        time.sleep(random.uniform(0.4, 1.1))


# ══════════════════════════════════════════════════════════════════════════════
# Declarative site profiles
# ══════════════════════════════════════════════════════════════════════════════
#
#   url            callable(keywords, page) -> start URL
#   capture        regex matched against XHR response URLs
#   rows           dotted path to the list of postings inside a payload
#   map            output field -> dotted path within a row
#   wait_for       optional selector to wait on before capturing
#   scroll_passes  wheel events, for lazily rendered lists
#   note           honest expectation, surfaced by --list
#
# Dotted paths accept numeric indices: "locations.0.label".

def _dig(obj: Any, path: str, default: Any = "") -> Any:
    cur = obj
    if not path:
        return default
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return default
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            return default
    return default if cur is None else cur


def load_learned() -> dict:
    try:
        return json.loads(LEARNED_PATH.read_text())
    except Exception:                                            # noqa: BLE001
        return {}


def save_learned(data: dict) -> None:
    try:
        LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEARNED_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    except Exception:                                            # noqa: BLE001
        pass


def _as_list(v: Any) -> list[str]:
    return [v] if isinstance(v, str) else list(v or [])


def _pick_card_selector(page: Any, candidates: list[str],
                        remembered: str = "") -> tuple[str, list]:
    """Choose the selector that finds the most cards.

    Most-matches rather than first-match: on Indeed, several selectors match
    something, but only one matches the actual result rows. Trying the
    remembered one first keeps the common case to a single query.
    """
    ordered = ([remembered] + [c for c in candidates if c != remembered]
               if remembered else candidates)
    best, best_els = "", []
    for sel in ordered:
        try:
            els = page.query_selector_all(sel)
        except Exception:                                        # noqa: BLE001
            continue
        if len(els) > len(best_els):
            best, best_els = sel, els
        # A remembered selector that still works is good enough; stop probing.
        if sel == remembered and len(els) >= 3:
            break
    return best, best_els


def _field_text(card: Any, candidates: list[str],
                remembered: str = "") -> tuple[str, str]:
    ordered = ([remembered] + [c for c in candidates if c != remembered]
               if remembered else candidates)
    for sel in ordered:
        try:
            el = card.query_selector(sel)
        except Exception:                                        # noqa: BLE001
            continue
        if el:
            try:
                txt = (el.get_attribute("title")
                       or el.inner_text() or "").strip()
            except Exception:                                    # noqa: BLE001
                continue
            if txt:
                return txt, sel
    return "", ""


def extract_dom(page: Any, site: str, spec: dict, learned: dict) -> list[dict]:
    """Read postings straight out of the rendered page.

    The counterpart to capture_json: needed when a site server-renders results
    and never issues a search XHR (Seek), or renders client-side but exposes no
    usable JSON to intercept (Indeed).
    """
    mem = learned.setdefault(site, {})
    card_sel, cards = _pick_card_selector(
        page, _as_list(spec.get("card")), mem.get("card", ""))
    if not cards:
        return []
    mem["card"] = card_sel

    id_spec = spec.get("id_from") or {}
    id_rx = re.compile(id_spec["regex"]) if id_spec.get("regex") else None

    out: list[dict] = []
    for card in cards:
        rec: dict[str, str] = {}
        for field, cands in (spec.get("fields") or {}).items():
            txt, sel = _field_text(card, _as_list(cands), mem.get(field, ""))
            if sel:
                mem[field] = sel
            rec[field] = txt
        if not rec.get("title"):
            continue

        ext = ""
        if id_spec:
            try:
                el = card.query_selector(id_spec.get("sel", "a"))
                raw = (el.get_attribute(id_spec.get("attr", "href")) or ""
                       ) if el else ""
                m = id_rx.search(raw) if id_rx else None
                ext = m.group(1) if m else raw
            except Exception:                                    # noqa: BLE001
                ext = ""
        if not ext:
            # Stable enough to dedupe a single run without a real id.
            ext = f"{rec.get('title','')}|{rec.get('company','')}"[:120]
        rec["ext_id"] = ext
        out.append(rec)
    return out


SITE_PROFILES: dict[str, dict] = {
    "seek-nz": {
        "label": "Seek New Zealand",
        "url": lambda kw, page: (
            f"https://www.seek.co.nz/{kw.replace(' ', '-')}-jobs?page={page}"),
        "capture": r"/api/jobsearch/v\d+/search",
        "rows": "data",
        "map": {"ext_id": "id", "title": "title", "company": "companyName",
                "location": "locations.0.label", "salary": "salaryLabel",
                "posted": "listingDate", "description": "teaser"},
        "url_tpl": "https://www.seek.co.nz/job/{ext_id}",
        "scroll_passes": 2,
        "note": "DO NOT USE. Measured 2026-08-02: Seek server-renders the "
                "results page and issues no search XHR, so there is nothing "
                "to capture. Its JSON API is public - sources.seek_search() "
                "gets the same data far faster.",
    },
    "zeil": {
        "label": "Zeil",
        "url": lambda kw, page: (
            f"https://www.zeil.com/jobs?search={kw.replace(' ', '%20')}"),
        # Bundle is obfuscated with no recoverable endpoint strings, so match
        # broadly and let the capture tell us what it actually calls.
        "capture": r"(api|graphql|search|jobs).*\.(json|graphql)|/api/",
        "rows": "jobs",
        "map": {"ext_id": "id", "title": "title", "company": "company.name",
                "location": "location", "salary": "salaryRange",
                "posted": "createdAt", "description": "description"},
        "url_tpl": "https://www.zeil.com/jobs/{ext_id}",
        "scroll_passes": 4,
        "note": "Obfuscated bundle. Capture pattern is deliberately wide; "
                "confirm the schema with --demo zeil.",
    },
    "indeed-nz": {
        "label": "Indeed New Zealand",
        "url": lambda kw, page: (
            f"https://nz.indeed.com/jobs?q={kw.replace(' ', '+')}"
            f"&start={(page - 1) * 10}"),
        # Indeed only honours fromage=1/3/7/14; anything else is ignored
        # silently and you get the unfiltered list back, so round up to the
        # nearest bucket it accepts rather than passing the raw request.
        "fresh_param": lambda days: "fromage={}".format(
            min((d for d in (1, 3, 7, 14) if d >= days), default=14)),
        # Verified working 2026-08-02: plain HTTP gets a hard 403 on the first
        # request, but landing on the homepage and typing the query returns 506
        # responses with no 403 at all and 16 result cards per page.
        "warmup": "https://nz.indeed.com",
        "search_selector": "input#text-input-what, input[name=q]",
        "requires_headed": True,
        # Pinned: the signed-in session lives in the chromium profile.
        "engine": "chromium",
        "settle": 6.0,
        "scroll_passes": 3,
        "dom": {
            "card": [".job_seen_beacon", "td.resultContent", ".cardOutline",
                     "#mosaic-provider-jobcards li"],
            "fields": {
                "title": ["h2 span[title]", "span[id^=jobTitle-]",
                          "h2.jobTitle", ".jobTitle"],
                "company": ["[data-testid=company-name]", ".companyName"],
                "location": ["[data-testid=text-location]",
                             ".companyLocation"],
                "salary": ["[data-testid=attribute_snippet_testid]",
                           ".salary-snippet-container", ".metadata.salary"],
                "posted": ["[data-testid=myJobsStateDate]",
                           "[data-testid=timing-attribute]", ".date"],
                "description": ["[data-testid=belowJobSnippet]",
                                ".job-snippet", "ul"],
            },
            "id_from": {"sel": "a[href*='jk=']", "attr": "href",
                        "regex": r"jk=([0-9a-f]+)"},
        },
        "url_tpl": "https://nz.indeed.com/viewjob?jk={ext_id}",
        "next_selectors": ("a[data-testid='pagination-page-next']",
                           "a[aria-label='Next Page']"),
        "note": "Requires headed mode AND a signed-in session. Headless "
                "returns 0 cards where headed returns 16, so Indeed detects "
                "headless Chrome; and clicking to page 2 redirects to "
                "secure.indeed.com/auth with 'To see more than one page of "
                "jobs, create an account or sign in'. Run "
                "`render.py --login indeed-nz` once. Reads the DOM - there is "
                "no usable JSON to intercept.",
    },
}


def _profile_url(prof: dict, keywords: str, page: int, since_days: int) -> str:
    """Profile URL for a page, with the site's freshness filter appended.

    Server-side filtering is an optimisation only: it saves fetching pages of
    stale postings. Correctness comes from discovery's own date filter, which
    applies to every source including the ones with no such parameter.
    """
    url = prof["url"](keywords, page)
    fresh = prof.get("fresh_param")
    if since_days > 0 and fresh:
        url += ("&" if "?" in url else "?") + fresh(since_days)
    return url


def fetch_profile(name: str, keywords: str, *, max_jobs: int = 100,
                  max_pages: int = 3, headless: bool = True,
                  engine: str = "chromium", device: str = "desktop",
                  since_days: int = 0,
                  session: BrowserSession | None = None,
                  on_payload: Callable[[str, Any], None] | None = None,
                  on_page: Callable[[str, int, int, int], None] | None = None,
                  ) -> list[dict]:
    """Run one site profile and return normalised job dicts.

    Shares the caller's session when given one, so a multi-site scan pays the
    browser launch cost once.
    """
    prof = SITE_PROFILES.get(name)
    if not prof:
        raise KeyError(f"unknown profile: {name}")

    # Some sites reject headless outright, so honour the profile over the
    # caller rather than silently returning nothing.
    if prof.get("requires_headed"):
        headless = False

    own = session is None
    sess = session or BrowserSession(headless=headless, engine=engine,
                                     device=device)
    if own:
        sess.start()

    jobs: list[dict] = []
    seen: set[str] = set()
    learned = load_learned()
    dom_spec = prof.get("dom")

    if dom_spec:
        def page_note(n: int, fresh: int, total: int) -> None:
            print(f"    page {n}: +{fresh} (total {total})", file=sys.stderr)
            if on_page:
                on_page(name, n, fresh, total)

        try:
            rows = sess.harvest_dom(
                _profile_url(prof, keywords, 1, since_days),
                name, dom_spec, learned,
                max_pages=max_pages, max_jobs=max_jobs,
                warmup=prof.get("warmup", ""),
                search_selector=prof.get("search_selector", ""),
                search_text=keywords,
                wait_for=prof.get("wait_for", ""),
                scroll_passes=prof.get("scroll_passes", 0),
                settle=prof.get("settle", 2.0),
                next_selectors=prof.get("next_selectors", ()),
                on_page=page_note,
            )
        finally:
            save_learned(learned)
            if own:
                sess.close()
        for rec in rows:
            ext = str(rec.get("ext_id") or "")
            if not ext or ext in seen:
                continue
            seen.add(ext)
            jobs.append({
                "id": f"{name}:{ext}", "source": name,
                "company": rec.get("company", ""),
                "title": rec.get("title", "").strip(),
                "location": rec.get("location", ""),
                "url": prof.get("url_tpl", "").format(ext_id=ext),
                "posted": rec.get("posted", ""),
                "salary": rec.get("salary", ""),
                "remote": None,
                "description": rec.get("description", ""),
            })
        return jobs[:max_jobs]

    try:
        for page_no in range(1, max_pages + 1):
            if len(jobs) >= max_jobs:
                break
            url = _profile_url(prof, keywords, page_no, since_days)

            payloads = sess.capture_json(
                url, prof["capture"],
                wait_for=prof.get("wait_for", ""),
                scroll_passes=prof.get("scroll_passes", 0),
                settle=prof.get("settle", 2.0),
                # Only prime and type on the first page - later pages reuse the
                # session and paginate from there.
                warmup=prof.get("warmup", "") if page_no == 1 else "",
                search_selector=(prof.get("search_selector", "")
                                 if page_no == 1 else ""),
                search_text=keywords if page_no == 1 else "",
            )
            if on_payload:
                for p in payloads:
                    on_payload(name, p)

            before = len(jobs)
            # A site rarely has exactly one endpoint worth reading. Accepting a
            # list of candidate paths means one profile can absorb the search
            # feed, the recommendations feed and whatever else it emits,
            # instead of failing when the primary one does not fire.
            row_paths = prof["rows"]
            if isinstance(row_paths, str):
                row_paths = [row_paths]
            for payload in payloads:
                rows = next((r for r in (_dig(payload, p, []) for p in row_paths)
                             if isinstance(r, list) and r), [])
                if not rows:
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rec = {k: _dig(row, p) for k, p in prof["map"].items()}
                    ext = str(rec.get("ext_id") or "")
                    if not ext or ext in seen:
                        continue
                    seen.add(ext)
                    jobs.append({
                        "id": f"{name}:{ext}",
                        "source": name,
                        "company": str(rec.get("company") or ""),
                        "title": str(rec.get("title") or "").strip(),
                        "location": str(rec.get("location") or ""),
                        "url": prof.get("url_tpl", "").format(ext_id=ext),
                        "posted": str(rec.get("posted") or ""),
                        "salary": str(rec.get("salary") or ""),
                        "remote": None,
                        "description": str(rec.get("description") or ""),
                    })
                    if len(jobs) >= max_jobs:
                        break
            # A page that adds nothing means the schema drifted or results ran
            # out; either way, stop paging rather than hammering.
            if len(jobs) == before:
                break
    finally:
        if own:
            sess.close()
    return jobs[:max_jobs]


def fetch_profiles(names: Iterable[str], keywords: str, *,
                   max_jobs: int = 100, max_pages: int = 4,
                   headless: bool = True, engine: str = "rotate",
                   device: str = "desktop", since_days: int = 0,
                   on_page: Callable[[str, int, int, int], None] | None = None,
                   ) -> list[dict]:
    """Run several profiles, one browser per site.

    engine='rotate' cycles chromium/firefox/webkit across sites; pass a single
    engine name to pin it. A launch per site costs a second or two but means one
    wedged browser cannot strand the rest of the scan.
    """
    if not available():
        return []
    nxt = engine_cycle()
    out: list[dict] = []
    for n in names:
        if n not in SITE_PROFILES:
            continue
        prof = SITE_PROFILES[n]
        # Each engine keeps its own profile directory, so a session signed in
        # under Chromium does not exist under Firefox. A site that needs a
        # login must therefore pin its engine - rotating would silently drop
        # it back to signed-out and return nothing.
        if prof.get("engine"):
            eng = _resolve_engine(prof["engine"])
        else:
            eng = nxt() if engine == "rotate" else _resolve_engine(engine)
        dev = prof.get("device", device)
        # The session is built here, so requires_headed has to be honoured
        # here too - checking it inside fetch_profile would be too late, the
        # browser would already be running headless.
        head = False if prof.get("requires_headed") else headless
        try:
            with BrowserSession(headless=head, engine=eng,
                                device=dev) as sess:
                got = fetch_profile(n, keywords, max_jobs=max_jobs,
                                    max_pages=max_pages, session=sess,
                                    since_days=since_days, on_page=on_page)
            print(f"  {n:<12} {eng:<9} {dev:<8} "
                  f"{'headed' if not head else 'headless':<8} "
                  f"{len(got):>4} jobs", file=sys.stderr)
            out.extend(got)
        except Exception as exc:                                 # noqa: BLE001
            print(f"  !! {n} ({eng}/{dev}): {exc}", file=sys.stderr)
    return out


LOGIN_URLS = {
    "indeed-nz": "https://nz.indeed.com/account/login",
    "seek-nz":   "https://www.seek.co.nz/oauth/login/",
    "zeil":      "https://www.zeil.com/login",
}


def interactive_login(site: str, *, engine: str = "chromium",
                      device: str = "desktop") -> bool:
    """Open a real window on the site's sign-in page and wait for the user.

    Deliberately manual. Credentials are typed by the user into the browser,
    never passed through this code, and 2FA or a captcha is handled the same
    way any person would. All that persists afterwards is the browser's own
    profile directory, which the next scan reuses.
    """
    url = LOGIN_URLS.get(site)
    if not url:
        print(f"no login URL known for {site}", file=sys.stderr)
        return False

    sess = BrowserSession(headless=False, engine=engine, device=device,
                          persist=True, slow_mo=60)
    sess.start()
    try:
        page = sess._ctx.pages[0] if sess._ctx.pages else sess._ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        sess.dismiss_consent(page)
        print(f"\n  A browser window is open at {url}")
        print(f"  Sign in there, then press Enter here to save the session.")
        print(f"  Profile: {sess.profile_dir()}\n")
        try:
            input("  press Enter when signed in > ")
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelled", file=sys.stderr)
            return False
        # Give the browser a moment to flush cookies to the profile on disk.
        time.sleep(2)
        print(f"  session saved to {sess.profile_dir()}")
        return True
    finally:
        sess.close()


def compare_devices(name: str, keywords: str, *, max_jobs: int = 40,
                    engine: str = "chromium", headless: bool = True,
                    ) -> dict[str, dict]:
    """Run one profile as desktop and as mobile and report which did better.

    Whether a phone view scrapes more cleanly is a property of the site, not
    something to assume - this measures it instead. Pin the winner into the
    profile's "device" key once you know.
    """
    results: dict[str, dict] = {}
    for dev in DEVICES:
        t0 = time.monotonic()
        try:
            jobs = fetch_profile(name, keywords, max_jobs=max_jobs,
                                 engine=engine, device=dev, headless=headless)
            err = ""
        except Exception as exc:                                 # noqa: BLE001
            jobs, err = [], str(exc)
        described = sum(1 for j in jobs if len(j.get("description", "")) > 200)
        results[dev] = {
            "jobs": len(jobs),
            "with_description": described,
            "seconds": round(time.monotonic() - t0, 1),
            "error": err,
        }
    best = max(results, key=lambda d: (results[d]["jobs"],
                                       results[d]["with_description"]))
    results["winner"] = {"device": best}
    return results


# ── cli ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grapply browser layer")
    ap.add_argument("--list", action="store_true", help="show site profiles")
    ap.add_argument("--demo", default="", help="run one profile, headed")
    ap.add_argument("--keywords", default="c++")
    ap.add_argument("--max-jobs", type=int, default=40)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--headless", action="store_true",
                    help="run without a visible window")
    ap.add_argument("--engine", default="chromium",
                    choices=[*ENGINES, "random", "rotate"],
                    help="browser engine (default chromium)")
    ap.add_argument("--device", default="desktop", choices=[*DEVICES],
                    help="desktop or mobile viewport")
    ap.add_argument("--compare-devices", action="store_true",
                    help="run desktop and mobile, report which scrapes better")
    ap.add_argument("--login", default="",
                    help="open a window to sign in to a site; the session is "
                         "reused by later scans")
    ap.add_argument("--no-persist", action="store_true",
                    help="use a throwaway profile, ignoring any saved sign-in")
    ap.add_argument("--dump", action="store_true",
                    help="print captured payload keys - use when a profile's "
                         "row paths need confirming")
    args = ap.parse_args(argv)

    if args.login:
        if not available():
            print(why_unavailable(), file=sys.stderr)
            return 2
        return 0 if interactive_login(args.login,
                                      engine=_resolve_engine(args.engine),
                                      device=args.device) else 1

    if args.list or not args.demo:
        print(f"playwright: {'ready' if available() else why_unavailable()}\n")
        for k, p in SITE_PROFILES.items():
            print(f"  {k:<12} {p['label']}")
            print(f"  {'':<12} {p['note']}\n")
        return 0

    if not available():
        print(why_unavailable(), file=sys.stderr)
        return 2

    def dump(site: str, payload: Any) -> None:
        if isinstance(payload, dict):
            print(f"  [{site}] payload keys: {list(payload)[:12]}",
                  file=sys.stderr)

    eng = _resolve_engine(args.engine)

    if args.compare_devices:
        res = compare_devices(args.demo, args.keywords,
                              max_jobs=args.max_jobs, engine=eng,
                              headless=args.headless)
        print(f"\n{args.demo} via {eng}\n")
        for dev in DEVICES:
            r = res[dev]
            note = f"  ERROR {r['error'][:50]}" if r["error"] else ""
            print(f"  {dev:<8} {r['jobs']:>4} jobs  "
                  f"{r['with_description']:>4} with JD  "
                  f"{r['seconds']:>6}s{note}")
        print(f"\n  winner: {res['winner']['device']}\n")
        return 0

    print(f"engine: {eng}   device: {args.device}   profile: {args.demo}   "
          f"headless: {args.headless}", file=sys.stderr)
    t0 = time.monotonic()
    with BrowserSession(headless=args.headless, engine=eng,
                        device=args.device,
                        persist=not args.no_persist) as sess:
        jobs = fetch_profile(args.demo, args.keywords, max_jobs=args.max_jobs,
                             max_pages=args.pages, session=sess,
                             on_payload=dump if args.dump else None)
    dt = time.monotonic() - t0
    print(f"\n{len(jobs)} jobs from {args.demo} via {eng}/{args.device} "
          f"in {dt:.1f}s\n")
    for j in jobs[:25]:
        print(f"  {j['title'][:52]:<54} {j['company'][:22]:<24} "
              f"{j['location'][:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
