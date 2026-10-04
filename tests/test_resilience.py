"""Tests for retry behaviour (src/resilience.py) as used by the LLM providers."""
import io
import urllib.error

import pytest

import src.extraction.providers as providers
import src.resilience as resilience


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(resilience.time, "sleep", calls.append)
    return calls


def test_transient_errors_are_retried(sleeps):
    attempts = []

    @resilience.with_retries(max_retries=2, base_delay=2.0)
    def flaky():
        attempts.append(1)
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        flaky()
    assert len(attempts) == 3
    assert sleeps == [2.0, 4.0]


def test_non_retryable_errors_fail_immediately(sleeps):
    attempts = []

    @resilience.with_retries(max_retries=2, no_retry_on=(KeyError,))
    def fails():
        attempts.append(1)
        raise KeyError("no key")

    with pytest.raises(KeyError):
        fails()
    assert len(attempts) == 1
    assert sleeps == []


def test_quota_exhausted_is_not_retried(monkeypatch, sleeps):
    attempts = []

    def urlopen(req, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"rate_limit"))

    monkeypatch.setattr(providers.settings, "open_router_api_key", "sk-test")
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)

    with pytest.raises(providers.QuotaExhaustedError):
        providers.call_openrouter_api("text")
    assert len(attempts) == 1
    assert sleeps == []


def test_missing_key_is_not_retried(monkeypatch, sleeps):
    monkeypatch.setattr(providers.settings, "groq_api_key", "")

    with pytest.raises(providers.ProviderConfigError):
        providers.call_groq_api("text")
    assert sleeps == []


def test_breaker_opens_and_recovers_after_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(resilience.time, "time", lambda: now[0])
    breaker = resilience.CircuitBreaker("test", failure_threshold=2, cooldown_seconds=60)

    breaker.record_failure()
    assert breaker.can_execute()
    breaker.record_failure()
    assert breaker.state == "OPEN"
    assert not breaker.can_execute()

    now[0] += 61
    assert breaker.can_execute()
    assert breaker.state == "HALF_OPEN"
    breaker.record_success()
    assert breaker.state == "CLOSED"


def test_half_open_lets_only_one_probe_through(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(resilience.time, "time", lambda: now[0])
    breaker = resilience.CircuitBreaker("test", failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()

    now[0] += 61
    assert breaker.can_execute()
    assert not breaker.can_execute()  # a second worker must not probe as well

    breaker.record_failure()  # the probe failed: back to OPEN for a full cooldown
    assert breaker.state == "OPEN"
    assert not breaker.can_execute()


def test_abandoned_probe_is_replaced_after_cooldown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(resilience.time, "time", lambda: now[0])
    breaker = resilience.CircuitBreaker("test", failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()

    now[0] += 61
    assert breaker.can_execute()  # this probe never reports back
    now[0] += 61
    assert breaker.can_execute()
def test_circuit_breaker_counts_concurrent_failures():
    import threading

    breaker = resilience.CircuitBreaker("test", failure_threshold=3)

    def fail_many():
        for _ in range(500):
            breaker.record_failure()

    threads = [threading.Thread(target=fail_many) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert breaker.failures == 8 * 500
    assert breaker.state == "OPEN"
    assert not breaker.can_execute()
