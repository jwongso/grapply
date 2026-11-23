"""
grapply - render: the Indeed Germany profile.

Offline only: the browser is replaced by fakes. These lock the three defects
found on 2026-11-24 - no location typed (so a Berlin scan searched all of
Germany), card text scraped as a posting date, and the age filter never
applied after a typed search.
"""
import render as R


def test_profile_url_carries_place_and_age_filter():
    prof = R.SITE_PROFILES["indeed-de"]
    url = R._profile_url(prof, "c++", 1, 7)
    assert url.startswith("https://de.indeed.com/jobs?q=c++")
    assert "l=Berlin" in url
    assert "fromage=7" in url


def test_profile_url_place_can_be_overridden_and_is_encoded():
    prof = R.SITE_PROFILES["indeed-de"]
    url = R._profile_url(prof, "c++", 1, 0, where="Frankfurt am Main")
    assert "l=Frankfurt+am+Main" in url
    assert "fromage" not in url


def test_profiles_without_a_where_param_are_unchanged():
    prof = R.SITE_PROFILES["indeed-nz"]
    assert "l=" not in R._profile_url(prof, "c++", 1, 0)


def test_indeed_de_declares_a_place_box_and_embedded_dates():
    prof = R.SITE_PROFILES["indeed-de"]
    assert prof["where"] == "Berlin" and prof["where_selector"]
    assert prof["dom"]["mosaic_dates"] is True
    # the card text is never a date source: its date-like text is "viewed
    # 20 days ago" and timing-attribute holds company + location
    assert "posted" not in prof["dom"]["fields"]
    assert prof["refine_after_search"] is True


class _FakePage:
    def __init__(self, rows):
        self._rows = rows

    def evaluate(self, js):
        return self._rows


def test_mosaic_dates_converts_epoch_ms_to_utc_iso():
    page = _FakePage([["abc", 1794204000000], ["nodate", None], ["", 5]])
    got = R.BrowserSession.mosaic_dates(page)
    assert got == {"abc": "2026-11-09T06:00:00+00:00"}


def test_mosaic_dates_is_empty_when_the_page_has_no_json():
    class Boom:
        def evaluate(self, js):
            raise RuntimeError("page closed")
    assert R.BrowserSession.mosaic_dates(Boom()) == {}
    assert R.BrowserSession.mosaic_dates(_FakePage(None)) == {}


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.kw = {}

    def harvest_dom(self, url, site, spec, learned, **kw):
        self.kw = dict(kw, url=url)
        return self.rows

    def close(self):
        pass


def _fetch(rows, since_days=7, **kw):
    sess = _FakeSession(rows)
    out = R.fetch_profile("indeed-de", "c++", session=sess,
                          since_days=since_days, **kw)
    return sess, out


def _row(ext, posted="2026-11-20T00:00:00+00:00", **kw):
    base = dict(ext_id=ext, title="Senior C++ Engineer", company="Acme",
                location="Berlin", posted=posted, salary="", description="")
    base.update(kw)
    return base


def test_fetch_profile_passes_place_and_clean_age_url_to_the_session():
    sess, _ = _fetch([_row("a")])
    assert sess.kw["where_text"] == "Berlin"
    assert sess.kw["where_selector"]
    assert "fromage=7" in sess.kw["refine_url"]
    assert "l=Berlin" in sess.kw["refine_url"]
    assert "vjk" not in sess.kw["refine_url"]


def test_fetch_profile_skips_the_age_step_when_no_window_is_requested():
    sess, _ = _fetch([_row("a")], since_days=0)
    assert sess.kw["refine_url"] == ""


def test_fetch_profile_where_override_reaches_the_session():
    sess, _ = _fetch([_row("a")], where="Hamburg")
    assert sess.kw["where_text"] == "Hamburg"
    assert "l=Hamburg" in sess.kw["refine_url"]


def test_board_date_is_labelled_as_a_board_date_not_an_employer_date():
    _, out = _fetch([_row("a"), _row("b", posted="", title="Other")])
    by = {j["id"]: j for j in out}
    assert by["indeed-de:a"]["posted_kind"] == "board_posted"
    assert by["indeed-de:b"]["posted_kind"] == "unknown"
    assert by["indeed-de:a"]["posted_source_url"].endswith("jk=a")


def test_dateless_sponsored_copy_of_a_dated_listing_is_dropped():
    _, out = _fetch([_row("real"), _row("ad", posted="")])
    assert [j["id"] for j in out] == ["indeed-de:real"]


def test_dateless_listing_with_no_dated_twin_is_kept_as_unknown():
    _, out = _fetch([_row("solo", posted="")])
    assert [j["id"] for j in out] == ["indeed-de:solo"]
