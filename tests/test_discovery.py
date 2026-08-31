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
