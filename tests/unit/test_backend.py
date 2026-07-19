import pytest

from lb.backend import Backend


def test_valid_backend_construction():
    b = Backend("http://localhost:9001", weight=3)
    assert b.host == "localhost"
    assert b.port == 9001
    assert b.weight == 3
    assert b.is_alive is True


def test_default_weight_is_one():
    b = Backend("http://localhost:9001")
    assert b.weight == 1


def test_default_port_for_http():
    b = Backend("http://example.com")
    assert b.port == 80


def test_default_port_for_https():
    b = Backend("https://example.com")
    assert b.port == 443


@pytest.mark.parametrize("bad_url", ["not-a-url", "", "localhost:9001", "ftp://"])
def test_invalid_url_raises(bad_url):
    with pytest.raises(ValueError):
        Backend(bad_url)


def test_set_alive_toggles_state():
    b = Backend("http://localhost:9001")
    b.set_alive(False)
    assert b.is_alive is False
    b.set_alive(True)
    assert b.is_alive is True


def test_str_returns_url():
    b = Backend("http://localhost:9001")
    assert str(b) == "http://localhost:9001"


def test_repr_includes_key_fields():
    b = Backend("http://localhost:9001", weight=2)
    r = repr(b)
    assert "localhost:9001" in r
    assert "weight=2" in r
    assert "alive=True" in r


def test_active_connections_defaults_to_zero():
    b = Backend("http://localhost:9001")
    assert b.active_connections == 0


def test_increment_connections():
    b = Backend("http://localhost:9001")
    b.increment_connections()
    b.increment_connections()
    assert b.active_connections == 2


def test_decrement_connections():
    b = Backend("http://localhost:9001")
    b.increment_connections()
    b.increment_connections()
    b.decrement_connections()
    assert b.active_connections == 1


def test_decrement_connections_clamped_at_zero():
    b = Backend("http://localhost:9001")
    b.decrement_connections()
    b.decrement_connections()
    assert b.active_connections == 0


def test_consecutive_failures_starts_at_zero():
    b = Backend("http://localhost:9001")
    assert b.consecutive_failures == 0


def test_record_failure_increments_and_returns_count():
    b = Backend("http://localhost:9001")
    assert b.record_failure() == 1
    assert b.record_failure() == 2
    assert b.consecutive_failures == 2


def test_record_success_resets_failure_streak():
    b = Backend("http://localhost:9001")
    b.record_failure()
    b.record_failure()
    b.record_success()
    assert b.consecutive_failures == 0


def test_set_alive_true_clears_failure_streak():
    b = Backend("http://localhost:9001")
    b.record_failure()
    b.record_failure()
    b.set_alive(False)
    b.set_alive(True)
    assert b.consecutive_failures == 0


def test_seconds_since_last_state_change_is_nonnegative():
    b = Backend("http://localhost:9001")
    assert b.seconds_since_last_state_change >= 0
