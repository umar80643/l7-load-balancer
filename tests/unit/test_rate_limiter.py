import time

from lb.ratelimit import RateLimiter, TokenBucket


def test_bucket_allows_up_to_capacity_immediately():
    bucket = TokenBucket(capacity=5, refill_rate=1)
    for _ in range(5):
        assert bucket.try_consume() is True
    assert bucket.try_consume() is False


def test_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_rate=10)  # 10 tokens/sec
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False

    time.sleep(0.15)  # ~1.5 tokens should have refilled
    assert bucket.try_consume() is True


def test_bucket_never_exceeds_capacity():
    bucket = TokenBucket(capacity=3, refill_rate=100)
    time.sleep(0.1)  # would refill way more than capacity if unclamped
    consumed = 0
    while bucket.try_consume():
        consumed += 1
        if consumed > 10:
            break
    assert consumed == 3


def test_rate_limiter_tracks_separate_buckets_per_client():
    limiter = RateLimiter(requests_per_second=1, burst=1)
    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("1.2.3.4") is False
    # A different client must have its own independent bucket.
    assert limiter.allow("5.6.7.8") is True


def test_rate_limiter_tracked_clients_count():
    limiter = RateLimiter(requests_per_second=5, burst=5)
    limiter.allow("1.1.1.1")
    limiter.allow("2.2.2.2")
    limiter.allow("1.1.1.1")  # same client again, shouldn't add a new bucket
    assert limiter.tracked_clients == 2


def test_rate_limiter_denies_after_burst_exhausted():
    limiter = RateLimiter(requests_per_second=0.001, burst=2)
    assert limiter.allow("9.9.9.9") is True
    assert limiter.allow("9.9.9.9") is True
    assert limiter.allow("9.9.9.9") is False
