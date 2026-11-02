import pytest

from alditalk.state import State, heartbeat_age, write_heartbeat


def test_roundtrip(tmp_path):
    st = State.load(tmp_path / "s.json")
    st.add_reading(1000, 5000)
    st.record_claim_attempt(1000)
    st.record_claim(1000, 1024)
    st.save()
    again = State.load(tmp_path / "s.json")
    assert again.readings == [[1000, 5000]]
    assert again.total_claims == 1
    assert again.last_claim_attempt() == 1000


def test_corrupt_file_starts_fresh(tmp_path):
    (tmp_path / "s.json").write_text("{nope")
    assert State.load(tmp_path / "s.json").readings == []


def test_rate_over_window():
    st = State()
    st.add_reading(0, 3000)
    st.add_reading(300, 2900)
    st.add_reading(600, 2800)
    assert st.consumption_rate(600) == pytest.approx(20.0)  # 200 MB in 10 min


def test_rate_unknown_with_one_reading():
    st = State()
    st.add_reading(0, 3000)
    assert st.consumption_rate(0) is None


def test_rate_resets_after_volume_increase():
    st = State()
    st.add_reading(0, 1500)
    st.add_reading(300, 900)
    st.add_reading(600, 1900)  # claimed +1 GB
    assert st.readings == [[600, 1900]]
    assert st.consumption_rate(600) is None


def test_heartbeat(tmp_path):
    write_heartbeat(tmp_path, now=100.0)
    assert heartbeat_age(tmp_path, now=160.0) == pytest.approx(60.0)
    assert heartbeat_age(tmp_path / "missing") is None


def test_latest_api_can_ignore_cached_responses(settings):
    from alditalk.browser import BrowserSession

    b = BrowserSession(settings)
    path = "/scs/bff/scs-209-selfcare-dashboard-bff/selfcare-dashboard/v1/offers"
    b.api[path] = (100.0, {"v": "cached"}, False)  # served from Chrome's cache
    assert b.latest_api("/v1/offers", since=50) == {"v": "cached"}
    assert b.latest_api("/v1/offers", since=50, fresh_only=True) is None
    b.api[path] = (120.0, {"v": "fresh"}, True)
    assert b.latest_api("/v1/offers", since=50, fresh_only=True) == {"v": "fresh"}
    assert b.latest_api("/v1/offers", since=130, fresh_only=True) is None  # older than the last page load
