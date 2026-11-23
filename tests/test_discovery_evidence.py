"""Offline regressions for original dates, reposts and office geography."""
from datetime import datetime, timedelta, timezone

import pytest

import discovery as D
import sources as S


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate_discovery(tmp_path, monkeypatch):
    for attr in ("STATE_PATH", "LAST_PATH", "SOURCES_PATH", "CONFIG_PATH"):
        monkeypatch.setattr(D, attr, tmp_path / f"{attr}.json")


def job(**values):
    base = S._job(id="test:1", title="Senior Software Engineer", company="Acme",
                  location="Berlin, Germany", url="https://example.com/jobs/1",
                  description="C++ Linux performance, Docker and code review.")
    base.update(values)
    return base


def test_greenhouse_original_date_wins_over_recent_edit(monkeypatch):
    monkeypatch.setattr(S, "http_json", lambda *a, **k: {"jobs": [{
        "id": 8288981, "title": "Senior Software Engineer",
        "location": {"name": "Hamburg, Germany"},
        "absolute_url": "https://job-boards.greenhouse.io/fender/jobs/8288981",
        "first_published": "2026-10-19T09:37:46Z",
        "updated_at": "2026-11-12T10:00:00Z", "content": "C++ Qt",
    }]})
    posting, = S.ats_greenhouse("fender", "Fender")
    assert posting["posted"] == "2026-10-19T09:37:46Z"
    assert posting["posted_kind"] == "employer_first_published"
    assert posting["updated_at"] == "2026-11-12T10:00:00Z"
    assert D.filter_by_age([posting], 7, verified_only=True, now=NOW) == []


def test_greenhouse_missing_original_does_not_promote_edit_date(monkeypatch):
    monkeypatch.setattr(S, "http_json", lambda *a, **k: {"jobs": [{
        "id": 1, "updated_at": "2026-11-12T10:00:00Z",
        "location": {"name": "Berlin"},
    }]})
    posting, = S.ats_greenhouse("acme", "Acme")
    assert posting["posted"] == ""
    assert posting["posted_kind"] == "unknown"


def test_ashby_hybrid_is_not_fully_remote_and_publish_is_not_original(monkeypatch):
    monkeypatch.setattr(S, "http_json", lambda *a, **k: {"jobs": [{
        "id": "one", "title": "Senior Engineer", "location": "Munich",
        "isListed": True, "isRemote": True, "workplaceType": "Hybrid",
        "publishedAt": "2026-11-11T10:00:00Z",
        "secondaryLocations": [{"location": "Berlin"}],
    }, {"id": "hidden", "isListed": False}]})
    posting, = S.ats_ashby("acme", "Acme")
    assert posting["remote"] is False
    assert posting["workplace_type"] == "Hybrid"
    assert posting["posted_kind"] == "employer_last_published"
    assert "Berlin" in posting["locations"]
    assert not D._location_ok(posting, D.MARKETS["germany-remote"])[0]


def test_strict_freshness_separates_unverified_and_future_dates():
    rows = [
        job(id="original", posted="2026-11-11T10:00:00Z",
            posted_kind="employer_first_published"),
        job(id="board", posted="2026-11-12T10:00:00Z", posted_kind="board_posted"),
        job(id="republish", posted="2026-11-12T10:00:00Z",
            posted_kind="employer_last_published"),
        job(id="undated"),
        job(id="future", posted="2027-01-01T10:00:00Z",
            posted_kind="employer_first_published"),
        job(id="old", posted="2026-10-19T10:00:00Z",
            posted_kind="employer_first_published"),
    ]
    review = []
    got = D.filter_by_age(rows, 7, verified_only=True, review=review, now=NOW)
    assert [j["id"] for j in got] == ["original"]
    assert got[0]["freshness"]["status"] == "verified_fresh"
    assert {j["id"] for j in review} == {"board", "republish", "undated", "future"}
    assert all(j["review_reasons"] for j in review)
    assert rows[-1]["freshness"]["status"] == "old"


def test_strict_original_check_applies_even_without_age_limit():
    review = []
    assert D.filter_by_age([job()], 0, verified_only=True, review=review, now=NOW) == []
    assert len(review) == 1


def test_known_older_first_seen_prevents_a_last_publication_refresh():
    posting = job(posted="2026-11-12T10:00:00Z",
                  posted_kind="employer_last_published",
                  first_seen_at="2026-10-01T10:00:00Z")
    assert D.filter_by_age([posting], 7, verified_only=False, now=NOW) == []
    assert posting["freshness"]["status"] == "old"


def test_dedupe_prefers_original_employer_evidence_over_long_repost():
    original = job(id="gh:acme:1", source="greenhouse", description="C++",
                   posted="2026-10-19T10:00:00Z",
                   posted_kind="employer_first_published")
    repost = job(id="arbeitnow:one", source="arbeitnow", description="C++ " * 500,
                 posted="2026-11-12T10:00:00Z", posted_kind="board_posted")
    merged, = D.dedupe([repost, original])
    assert merged["id"] == original["id"]
    assert merged["board_posted"] == repost["posted"]
    assert D.filter_by_age([merged], 7, verified_only=True, now=NOW) == []


def test_dedupe_preserves_different_levels_and_offices():
    rows = [job(id="one", title="Senior Software Engineer"),
            job(id="two", title="Staff Software Engineer"),
            job(id="three", location="Hamburg, Germany")]
    assert len(D.dedupe(rows)) == 3


@pytest.mark.parametrize("city", ["Berlin", "Hamburg", "Frankfurt am Main", "München", "Munich"])
def test_approved_german_city_markets(city):
    assert D._location_ok(job(location=city), D.MARKETS["german-cities"])[0]


def test_other_city_and_onsite_conflict_do_not_pass_preferred_city_gate():
    assert not D._location_ok(job(location="Stuttgart"), D.MARKETS["german-cities"])[0]
    posting = job(location="Berlin", description=(
        "C++ Qt. We operate a hybrid working model. You must work from the "
        "Stuttgart office 2-3 times a week."))
    ok, why = D._location_ok(posting, D.MARKETS["german-cities"])
    assert not ok and "conflict" in why


def test_hamburg_office_does_not_qualify_for_berlin_only():
    posting = job(location="Berlin", description=(
        "Are you able to work from the Hamburg office 2-3 times a week?"))
    assert not D._location_ok(posting, D.MARKETS["berlin"])[0]
    # Hamburg is a valid independent target under the expanded preference.
    assert D._location_ok(job(location="Hamburg", description=posting["description"]),
                          D.MARKETS["german-cities"])[0]


def test_agnostic_ai_engineering_passes_without_language_keyword():
    posting = job(title="Senior AI Engineer", description=(
        "Design reliable LLM agents, retrieval and evaluation workflows. Own "
        "backend architecture, API design, testing, code review and mentoring. "
        "Use Docker and Git. Strong software engineering matters more than a language."))
    result = D.prefilter(posting, "berlin")
    assert not result["reject"]
    assert result["stack_match"] == "agnostic"
    assert result["prefilter_score"] >= 45
    assert any("agnostic" in n for n in result["notes"])


def test_explicit_different_language_is_not_mislabelled_agnostic():
    posting = job(title="Senior Ruby Developer", description=(
        "Must have expert Ruby and Rails experience. Build reliable backend APIs."))
    result = D.prefilter(posting, "berlin")
    assert result["stack_match"] == "other"
    assert result["reject"]


def test_agnostic_bypass_can_be_disabled_and_preserves_other_gates():
    posting = job(title="Senior AI Engineer", description="Build LLM agents and backend APIs.")
    assert D.prefilter(posting, "berlin", {"allow_stack_agnostic": False})["reject"]
    assert D.prefilter(dict(posting, title="Junior AI Engineer"), "berlin")["reject"]
    assert D.prefilter(dict(posting, location="Stuttgart"), "german-cities")["reject"]


def test_new_defaults_are_strict_and_existing_config_remains_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "CONFIG_PATH", tmp_path / "config.json")
    cfg = D.load_config()
    assert cfg["max_age_days"] == 7
    assert cfg["require_verified_posted"] is True
    assert cfg["allow_stack_agnostic"] is True
    cfg.update(max_age_days=0, require_verified_posted=False)
    D.save_config(cfg)
    assert D.load_config()["max_age_days"] == 0
    assert D.load_config()["require_verified_posted"] is False


def test_supported_url_verification_uses_only_public_fixed_api(monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return {"id": 8288981, "title": "Senior Engineer", "company_name": "Fender",
                "location": {"name": "Hamburg"}, "first_published": "2026-10-19",
                "updated_at": "2026-11-12", "content": "C++ Qt"}

    monkeypatch.setattr(S, "http_json", fake)
    result = S.verify_posting_url("https://job-boards.greenhouse.io/fender/jobs/8288981")
    assert result["posted"] == "2026-10-19"
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/fender/jobs/8288981"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/secrets", "file:///etc/passwd",
                               "https://job-boards.greenhouse.io.evil.test/acme/jobs/1",
                               "https://user:password@job-boards.greenhouse.io/acme/jobs/1",
                               "https://job-boards.greenhouse.io:444/acme/jobs/1"])
def test_verification_rejects_untrusted_targets_without_network(monkeypatch, url):
    monkeypatch.setattr(S, "http_json", lambda *a, **k: pytest.fail("network must not run"))
    with pytest.raises(ValueError):
        S.verify_posting_url(url)


def test_scan_dedupes_before_age_check_and_never_scores_review_jobs(monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(D, "datetime", FixedTime)
    cfg = dict(D.DEFAULT_CONFIG, phases=[{"name": "Preferred", "market": "german-cities",
                                        "use_registry": False}])
    monkeypatch.setattr(D, "load_config", lambda: cfg)
    monkeypatch.setattr(D, "load_sources", lambda: [])
    rows = [
        job(id="old-original", company="Old", posted="2026-10-01",
            posted_kind="employer_first_published"),
        job(id="old-repost", company="Old", posted="2026-11-12", posted_kind="board_posted"),
        job(id="fresh", company="Fresh", posted="2026-11-11",
            posted_kind="employer_first_published"),
        job(id="review", company="Review", posted="2026-11-11", posted_kind="board_posted"),
        job(id="conflict", company="Conflict", posted="2026-11-11",
            posted_kind="employer_first_published", description=(
                "C++ Linux performance. Must work from the Stuttgart office 3 days per week.")),
    ]
    monkeypatch.setattr(D, "gather_phase", lambda *a, **k: [dict(j) for j in rows])
    from types import SimpleNamespace
    config = SimpleNamespace(load=lambda: {}, load_resume_text=lambda cfg: "Test resume")
    monkeypatch.setattr(D, "_ai_bits", lambda: (None, None, config))
    scored = []
    monkeypatch.setattr(D, "llm_score", lambda rows, **kw: scored.extend(j["id"] for j in rows))
    payload = D.run_scan(do_llm=True, prefilter_min=0)
    assert scored == ["fresh"]
    assert [j["id"] for j in payload["jobs"]] == ["fresh"]
    assert {j["id"] for j in payload["review_jobs"]} == {"review", "conflict"}
    assert payload["counts"]["freshness_excluded"] == 1
    assert payload["counts"]["review"] == 2
    assert all("description" not in j for j in payload["review_jobs"])
    assert set(D._load_state()["seen"]) == {j["id"] for j in rows}


def test_dismissed_duplicate_cannot_return_from_another_source(monkeypatch):
    cfg = dict(D.DEFAULT_CONFIG, phases=[{"name": "Berlin", "market": "berlin",
                                        "use_registry": False}])
    monkeypatch.setattr(D, "load_config", lambda: cfg)
    monkeypatch.setattr(D, "load_sources", lambda: [])
    D.dismiss("board:old")
    rows = [job(id="employer:one", posted=NOW.isoformat(),
                posted_kind="employer_first_published"),
            job(id="board:old", posted=NOW.isoformat(), posted_kind="board_posted")]
    monkeypatch.setattr(D, "gather_phase", lambda *a, **k: rows)
    assert D.run_scan(do_llm=False)["jobs"] == []


def test_scan_remembers_previous_observation_across_republication(monkeypatch):
    cfg = dict(D.DEFAULT_CONFIG, phases=[{"name": "Berlin", "market": "berlin",
                                        "use_registry": False}])
    monkeypatch.setattr(D, "load_config", lambda: cfg)
    monkeypatch.setattr(D, "load_sources", lambda: [])
    D._save_state({"seen": {"one": "2020-01-01"}, "dismissed": {}, "applied": {}})
    rows = [job(id="one", posted=datetime.now(timezone.utc).isoformat(),
                posted_kind="employer_last_published")]
    monkeypatch.setattr(D, "gather_phase", lambda *a, **k: rows)
    result = D.run_scan(do_llm=False, verified_only=False)
    assert result["jobs"] == result["review_jobs"] == []
    assert result["counts"]["freshness_excluded"] == 1


@pytest.mark.parametrize("offset,accepted", [(0, True), (7, True), (7.001, False)])
def test_freshness_window_boundary(offset, accepted):
    posting = job(posted=(NOW - timedelta(days=offset)).isoformat(),
                  posted_kind="employer_first_published")
    assert bool(D.filter_by_age([posting], 7, verified_only=True, now=NOW)) is accepted


def test_url_check_does_not_create_config_or_state(monkeypatch, capsys):
    monkeypatch.setattr(D.src_mod, "verify_posting_url", lambda url: job(
        posted="2020-01-01", posted_kind="employer_first_published"))
    assert D.main(["--check-url", "https://job-boards.greenhouse.io/acme/jobs/1"]) == 0
    assert '"status": "old"' in capsys.readouterr().out
    assert not D.STATE_PATH.exists()
    assert not D.CONFIG_PATH.exists()


def test_seen_toggle_is_independent_of_freshness(monkeypatch):
    cfg = dict(D.DEFAULT_CONFIG, phases=[{"name": "Berlin", "market": "berlin",
                                        "use_registry": False}])
    monkeypatch.setattr(D, "load_config", lambda: cfg)
    monkeypatch.setattr(D, "load_sources", lambda: [])
    row = job(posted=datetime.now(timezone.utc).isoformat(),
              posted_kind="employer_first_published")
    monkeypatch.setattr(D, "gather_phase", lambda *a, **k: [dict(row)])
    assert D.run_scan(do_llm=False)["jobs"]
    assert D.run_scan(do_llm=False, include_seen=False)["jobs"] == []
    assert D.run_scan(do_llm=False, include_seen=True)["jobs"]


def test_frankfurt_oder_is_not_the_approved_frankfurt():
    assert not D._location_ok(job(location="Frankfurt (Oder)"), D.MARKETS["german-cities"])[0]
    assert D._location_ok(job(location="Frankfurt am Main"), D.MARKETS["german-cities"])[0]


def test_distinct_employer_requisitions_survive_dedupe():
    old = job(id="gh:acme:1", source="greenhouse", posted="2020-01-01",
              posted_kind="employer_first_published", description="C++ " * 100)
    fresh = job(id="gh:acme:2", source="greenhouse", posted="2026-11-11",
                posted_kind="employer_first_published")
    rows = D.dedupe([old, fresh])
    assert len(rows) == 2
    assert [j["id"] for j in D.filter_by_age(rows, 7, verified_only=True, now=NOW)] == [fresh["id"]]


@pytest.mark.parametrize("office", ["Hamburg", "Stuttgart and Hamburg"])
def test_conflicting_office_cannot_borrow_another_approved_city(office):
    body = ("C++ Linux. You must work from the Hamburg office 3 days per week. "
            + ("You must work from the Stuttgart office 2 days per week." if "Stuttgart" in office else ""))
    ok, reason = D._location_ok(job(location="Berlin", description=body), D.MARKETS["german-cities"])
    assert not ok and "conflict" in reason


def test_creation_time_does_not_disprove_a_new_public_posting():
    review = []
    posting = job(posted="2020-01-01", posted_kind="employer_created")
    assert D.filter_by_age([posting], 7, verified_only=True, now=NOW, review=review) == []
    assert review == [posting]
    assert posting["freshness"]["status"] == "unverified"


@pytest.mark.parametrize("description,kind", [
    ("Any modern programming language, such as Java or Rust", "agnostic"),
    ("Not a language-agnostic position; expert Ruby and Rails required", "other"),
    ("Five years of professional Go experience required", "other"),
    ("Build backend APIs and LLM agents. Ruby exposure is optional", "agnostic"),
    ("Must have expert Ruby experience. Python is nice to have", "other"),
])
def test_stack_requirement_context(description, kind):
    result = D.prefilter(job(description=description), "berlin")
    assert result["stack_match"] == kind
    assert bool(result["reject"]) is (kind == "other")


def test_unknown_remote_eligibility_requires_location_review():
    posting = job(location="", remote=True)
    ok, reason = D._location_ok(posting, D.MARKETS["germany-remote"])
    assert not ok and "location conflict:" in reason
    posting["description"] = "C++ backend. Fully remote within Germany."
    assert D._location_ok(posting, D.MARKETS["germany-remote"])[0]


def test_explicit_work_rights_requirement_overrides_office_tag():
    posting = job(description="C++ backend. Must be authorized to work in the US.")
    assert not D._location_ok(posting, D.MARKETS["berlin"])[0]


def test_munich_alias_is_not_a_conflicting_office():
    posting = job(location="Munich", description="Must work from the München office 3 days per week.")
    assert D._location_ok(posting, D.MARKETS["german-cities"])[0]


def test_two_mandatory_offices_need_explicit_location_evidence():
    posting = job(location="Berlin", description=(
        "Must work from the Berlin office 3 days per week. "
        "Must work from the Hamburg office 2 days per week."))
    assert not D._location_ok(posting, D.MARKETS["german-cities"])[0]


@pytest.mark.parametrize("optional", ["Python is nice to have", "Rust is optional"])
def test_optional_clause_does_not_hide_a_mandatory_specialist(optional):
    assert D.prefilter(job(description=f"Must have expert Ruby experience, {optional}"),
                       "berlin")["stack_match"] == "other"


def test_canonical_requisition_merges_despite_board_location_variation():
    url = "https://job-boards.greenhouse.io/acme/jobs/1"
    original = job(id="gh:acme:1", source="greenhouse", url=url,
                   location="Berlin, Germany", posted="2020-01-01",
                   posted_kind="employer_first_published")
    board = job(id="board:one", source="linkedin", url=url + "?tracking=one",
                location="Berlin, Berlin, Germany", posted="2026-11-12", posted_kind="board_posted")
    merged, = D.dedupe([board, original])
    assert merged["id"] == original["id"]
    assert D.filter_by_age([merged], 7, verified_only=True, now=NOW) == []
