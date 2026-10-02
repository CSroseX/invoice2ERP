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
