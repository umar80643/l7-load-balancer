import time

from lb.circuitbreaker import CircuitBreaker, CircuitState


def test_starts_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=1.0)
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_stays_closed_below_failure_threshold():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=1.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_opens_after_failure_threshold_reached():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=1.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_success_resets_failure_count_while_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=1.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    # Only 2 consecutive failures since the reset -- should still be closed.
    assert cb.state == CircuitState.CLOSED


def test_transitions_to_half_open_after_recovery_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    time.sleep(0.1)
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_allows_exactly_one_probe_at_a_time():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    time.sleep(0.1)

    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow_request() is True
    # A second concurrent caller must be rejected while the first probe is
    # still in flight.
    assert cb.allow_request() is False


def test_half_open_success_closes_circuit():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.allow_request() is True  # consumes the trial probe slot

    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_half_open_failure_reopens_circuit():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.allow_request() is True  # consumes the trial probe slot

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_reopening_resets_recovery_timer():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.1)
    cb.record_failure()
    time.sleep(0.15)
    assert cb.allow_request() is True  # half-open probe
    cb.record_failure()  # probe fails, reopens

    # Immediately after reopening, should still be OPEN (timer restarted),
    # not yet HALF_OPEN again.
    assert cb.state == CircuitState.OPEN
