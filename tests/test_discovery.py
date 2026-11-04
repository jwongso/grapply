"""
grapply - discovery: markets, the location gate, and a dynamic-country scan.

These cover the part that decides *where* a posting counts as reachable, which
is now driven by the MARKETS table rather than a hard-coded NZ gate.
"""
import json

import pytest

import discovery as D
import sources as S


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Keep every test off the real ~/.grapply files."""
    for attr, fn in (("STATE_PATH", "discovery_state.json"),
                     ("LAST_PATH", "discovery_last.json"),
                     ("SOURCES_PATH", "sources.json"),
                     ("CONFIG_PATH", "discovery_config.json")):
        monkeypatch.setattr(D, attr, tmp_path / fn)
    yield


def _job(**kw):
    base = dict(id="x:1", source="test", company="Acme",
                title="Senior C++ Software Engineer", location="", url="",
                posted="", salary="", remote=None,
                description="We build low-latency systems in modern C++.")
    base.update(kw)
    return base


# ── resolve_market ──────────────────────────────────────────────────────────

def test_resolve_market_builtin_and_alias():
    assert D.resolve_market("germany")["label"] == "Germany"
    # legacy "gate" key still resolves
    assert D.resolve_market({"gate": "nz"})["label"] == "New Zealand"
    assert D.resolve_market({"market": "remote"})["require_remote"] is True


def test_resolve_market_none_and_blank():
    assert D.resolve_market(None) is None
    assert D.resolve_market("") is None
    assert D.resolve_market({}) is None


def test_resolve_market_unknown_raises():
    with pytest.raises(ValueError) as e:
        D.resolve_market("atlantis")
    assert "atlantis" in str(e.value)


def test_resolve_market_config_override_merges():
    cfg = {"markets": {"germany": {"in_market": ["Potsdam"]}}}
    m = D.resolve_market("germany", cfg)
    assert "potsdam" in m["in_market"]          # lowercased on the way in
    assert m["label"] == "Germany"              # untouched keys survive


def test_resolve_market_config_defines_new_market():
    cfg = {"markets": {"iceland": {"in_market": ["reykjavik"],
                                   "reachable_remote": ["europe"]}}}
    m = D.resolve_market("iceland", cfg)
    assert m["label"] == "Iceland"
    assert "reykjavik" in m["in_market"]


# ── the location gate, per market ───────────────────────────────────────────

GERMANY = D.MARKETS["germany"]
NZ = D.MARKETS["nz"]


def test_germany_accepts_on_site_berlin():
    ok, why = D._location_ok(_job(location="Berlin, Germany"), GERMANY)
    assert ok, why


def test_germany_accepts_remote_europe():
    ok, why = D._location_ok(
        _job(location="Remote (Europe)", remote=True), GERMANY)
    assert ok, why


def test_germany_rejects_remote_us_only():
    ok, why = D._location_ok(
        _job(location="Remote - United States", remote=True), GERMANY)
    assert not ok
    assert "region-locked" in why


def test_germany_rejects_us_work_authorisation_in_body():
    job = _job(location="Remote",
               description="Modern C++. You must be authorized to work in "
                           "the US.", remote=True)
    ok, why = D._location_ok(job, GERMANY)
    assert not ok
    assert "work rights" in why


def test_germany_accepts_blank_location_from_german_board():
    # arbeitnow is a German board; a region-less posting inherits its market.
    job = _job(source="arbeitnow", location="Homeoffice", remote=True)
    ok, why = D._location_ok(job, GERMANY)
    assert ok, why


def test_nz_still_rejects_blank_location_from_german_board():
    job = _job(source="arbeitnow", location="Homeoffice", remote=True)
    ok, why = D._location_ok(job, NZ)
    assert not ok
    assert "Germany" in why


def test_nz_accepts_auckland():
    ok, why = D._location_ok(_job(location="Auckland, New Zealand"), NZ)
    assert ok, why


def test_require_remote_market_rejects_on_site_role():
    ok, why = D._location_ok(
        _job(location="Berlin, Germany"), D.MARKETS["remote"])
    assert not ok
    assert "not remote" in why


def test_work_from_anywhere_body_overrides_hq_city():
    job = _job(location="Austin, Texas",
               description="Work from anywhere in the world. Modern C++.",
               remote=True)
    ok, why = D._location_ok(job, GERMANY)
    assert ok, why


def test_none_market_passes_everything():
    ok, why = D._location_ok(_job(location="Pyongyang"), None)
    assert ok and why == ""


# ── prefilter integration ──────────────────────────────────────────────────

def test_prefilter_accepts_market_as_string_or_dict():
    job = _job(location="Remote - United States", remote=True)
    by_str = D.prefilter(dict(job), "germany")
    by_obj = D.prefilter(dict(job), GERMANY)
    assert by_str["reject"] and by_obj["reject"]
    assert any("region-locked" in r for r in by_str["reject"])


def test_prefilter_no_market_does_not_gate_location():
    job = _job(location="Remote - United States", remote=True)
    res = D.prefilter(job, None)
    assert not any("region-locked" in r for r in res["reject"])


# ── synthetic phase + default config ───────────────────────────────────────

def test_default_config_phases_all_resolve():
    for p in D.DEFAULT_CONFIG["phases"]:
        assert D.resolve_market(p) is not None


def test_synthetic_phase_pulls_sources_from_market():
    ph = D._synthetic_phase("germany", GERMANY, D.DEFAULT_CONFIG)
    assert ph["market"] == "germany"
    assert ph["seek_sites"] == []                    # Germany has no Seek
    assert "arbeitnow" in ph["aggregators"]
    assert ph["keywords"]


# ── run_scan: a dynamic-country scan end to end ─────────────────────────────

def _fake_aggregators(names=None, on_source=None):
    jobs = [
        S._job(id="a:berlin", source="arbeitnow", company="BerlinCo",
               title="Senior C++ Software Engineer", location="Berlin, Germany",
               remote=False,
               description=("Modern C++17, STL, templates, RAII. Low-latency "
                            "multithreaded systems on embedded Linux. "
                            "Senior role, CMake, Docker, git.")),
        S._job(id="a:usonly", source="remoteok", company="UsCo",
               title="Senior C++ Software Engineer",
               location="Remote - United States", remote=True,
               description=("Modern C++. You must be authorized to work in "
                            "the US. STL, templates, CMake, Docker.")),
    ]
    if on_source:
        on_source("arbeitnow", len(jobs))
    return jobs


def test_run_scan_market_germany(monkeypatch):
    monkeypatch.setattr(S, "fetch_registry", lambda *a, **k: [])
    monkeypatch.setattr(S, "fetch_aggregators", _fake_aggregators)
    monkeypatch.setattr(D.src_mod, "fetch_registry", lambda *a, **k: [])
    monkeypatch.setattr(D.src_mod, "fetch_aggregators", _fake_aggregators)

    out = D.run_scan(do_llm=False, do_rank=False, market="germany")

    ids = {j["id"] for j in out["jobs"]}
    assert "a:berlin" in ids
    assert "a:usonly" not in ids
    assert out["counts"]["rejected"] >= 1
    # persisted where the UI reads it
    assert json.loads(D.LAST_PATH.read_text())["jobs"]


def test_run_scan_unknown_market_raises(monkeypatch):
    monkeypatch.setattr(D.src_mod, "fetch_registry", lambda *a, **k: [])
    with pytest.raises(ValueError):
        D.run_scan(do_llm=False, do_rank=False, market="narnia")


# ── CLI ────────────────────────────────────────────────────────────────────

def test_cli_list_markets(capsys):
    assert D.main(["--list-markets"]) == 0
    printed = capsys.readouterr().out
    assert "germany" in printed and "New Zealand" in printed


def test_cli_unknown_market_exits_nonzero(monkeypatch):
    monkeypatch.setattr(D.src_mod, "fetch_registry", lambda *a, **k: [])
    assert D.main(["--market", "narnia"]) == 2


# ── Germany: Berlin on-site + Germany remote ────────────────────────────────

BERLIN = D.MARKETS["berlin"]
DE_REMOTE = D.MARKETS["germany-remote"]


def test_dutch_town_de_lier_is_not_germany():
    # "de" used to be a Germany token and matched "De Lier".
    job = _job(location="De Lier, Zuid-Holland, Netherlands")
    assert not D._location_ok(job, BERLIN)[0]
    assert not D._location_ok(job, DE_REMOTE)[0]


def test_named_office_is_not_made_remote_by_the_body():
    body = "Work from anywhere in the world, worldwide team, remote culture."
    for loc in ("Amsterdam office", "Munich, Bavaria, Germany"):
        job = _job(location=loc, description=body)
        assert not D._location_ok(job, DE_REMOTE)[0], loc


def test_hybrid_role_in_other_city_is_rejected_for_remote_phase():
    job = _job(location="Munich, Bavaria, Germany",
               description="Able to be onsite in our offices 2-3x a week.")
    assert not D._location_ok(job, DE_REMOTE)[0]
    assert not D._location_ok(job, BERLIN)[0]


def test_berlin_phase_accepts_berlin_and_rejects_other_german_cities():
    assert D._location_ok(_job(location="Berlin, Berlin, Germany"), BERLIN)[0]
    assert not D._location_ok(_job(location="Hamburg"), BERLIN)[0]


def test_country_level_location_needs_plain_remote_statement():
    remote = _job(location="Germany",
                  description="This is a fully remote position within Germany.")
    hybrid = _job(location="Germany",
                  description="Hybrid working model, flexibility for remote work.")
    assert D._location_ok(remote, DE_REMOTE)[0]
    assert not D._location_ok(hybrid, DE_REMOTE)[0]


def test_board_flagged_remote_in_german_city_is_reachable():
    job = _job(location="Munich", remote=True)
    assert D._location_ok(job, DE_REMOTE)[0]


def test_german_titles_count_as_engineering_and_junior_is_rejected():
    ok = D.prefilter(_job(title="Softwareentwickler C++ (m/w/d)",
                          location="Berlin"), BERLIN)
    assert not any("not an engineering" in r for r in ok["reject"])
    junior = D.prefilter(_job(title="Werkstudent Softwareentwicklung C++",
                              location="Berlin"), BERLIN)
    assert any("junior" in r for r in junior["reject"])


def test_linkedin_phase_keeps_only_postings_located_in_the_city(monkeypatch):
    stubs = [
        _job(id="linkedin:1", source="linkedin", location="Berlin, Berlin, Germany"),
        _job(id="linkedin:2", source="linkedin", location="Munich, Bavaria, Germany"),
        _job(id="linkedin:3", source="linkedin", location="Germany"),
    ]
    seen = {}
    monkeypatch.setattr(S, "linkedin_search",
                        lambda kw, **k: seen.update(k) or list(stubs))
    monkeypatch.setattr(S, "linkedin_hydrate", lambda jobs, progress=None: jobs)
    phase = {"name": "t", "keywords": ["c++"], "linkedin": True,
             "aggregators": [], "seek_sites": [], "browser_profiles": [],
             "use_registry": False}
    got = D.gather_phase(phase, {"max_age_days": 7}, [], lambda s: None,
                         market=BERLIN)
    assert [j["id"] for j in got] == ["linkedin:1"]
    assert "remote" not in seen          # no LinkedIn remote filter
    assert seen["where"] == "Berlin, Germany"


def test_remote_phase_does_not_use_linkedin():
    assert not DE_REMOTE["linkedin"]
    phases = {p["market"]: p for p in D.DEFAULT_CONFIG["phases"]}
    assert not phases["germany-remote"].get("linkedin")
    assert phases["berlin"]["linkedin"]


# ── ranking and the "customers worldwide" leak ──────────────────────────────

def _fake_ai(reply):
    class _Ai:
        def __init__(self, cfg): pass
        def generate(self, system, user, **kw):
            _Ai.seen = user
            return reply
    class _Cfg:
        @staticmethod
        def load(): return {"ai": {}}
        @staticmethod
        def load_resume_text(cfg): return "resume"
    return _Ai, _Cfg


def _ranked_jobs():
    return [_job(id=f"arbeitnow:very-long-slug-{n}-berlin-9999{n}", llm_score=8.0 + n / 10,
                 title=f"Job {n}") for n in range(1, 5)]


def test_rank_candidates_numbers_have_no_gaps_when_ids_are_mangled(monkeypatch):
    jobs = _ranked_jobs()
    # The model echoes ids 1 and 3 correctly and mangles the other two.
    reply = json.dumps([
        {"rank": 1, "id": "garbled-a", "verdict": "strong", "why": "x"},
        {"rank": 2, "id": "1", "verdict": "strong", "why": "x"},
        {"rank": 3, "id": "garbled-b", "verdict": "worth it", "why": "x"},
        {"rank": 4, "id": "3", "verdict": "marginal", "why": "x"},
    ])
    ai, cfg = _fake_ai(reply)
    monkeypatch.setattr(D, "_ai_bits", lambda: (type("M", (), {"AIClient": ai}), None, cfg))
    out = D.rank_candidates(jobs, verbose=False)
    assert [j["rank"] for j in out] == [1, 2, 3, 4]
    assert [j["title"] for j in out[:2]] == ["Job 1", "Job 3"]
    # the two the model lost still appear, best fit first, with real numbers
    assert [j["title"] for j in out[2:]] == ["Job 4", "Job 2"]
    # the prompt uses short numeric ids, not the board slugs
    assert "id=1\n" in ai.seen and "very-long-slug" not in ai.seen.split("ROLES")[1].split("title=")[0]


def test_customers_worldwide_does_not_make_a_mexico_role_reachable():
    job = _job(location="Mexico", remote=True,
               description="Build impactful solutions for customers worldwide.")
    assert not D._location_ok(job, DE_REMOTE)[0]


def test_explicit_work_from_anywhere_still_overrides_hq_city():
    job = _job(location="Austin, Texas", remote=True,
               description="Work from anywhere in the world. Modern C++.")
    assert D._location_ok(job, DE_REMOTE)[0]
