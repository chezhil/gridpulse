"""LLM usage limits for public deployments."""
import pytest

from backend.mcp import quota


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    quota.reset()
    yield
    quota.reset()


def test_per_user_and_site_limits(monkeypatch):
    monkeypatch.setenv("GRIDPULSE_AI_PER_USER_10MIN", "2")
    monkeypatch.setenv("GRIDPULSE_AI_PER_HOUR", "3")
    quota.consume("A"); quota.consume("A")
    with pytest.raises(quota.QuotaExceeded, match="10 minutes"):
        quota.consume("A")
    quota.consume("B")
    with pytest.raises(quota.QuotaExceeded, match="/hour"):
        quota.consume("C")


def test_ai_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("GRIDPULSE_AI_ENABLED", "0")
    with pytest.raises(quota.QuotaExceeded, match="turned off"):
        quota.consume("A")
