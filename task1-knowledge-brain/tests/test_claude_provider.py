"""Claude on Amazon Bedrock (default) or Anthropic's API: client, model ids, configuration."""

import base64

import anthropic
import httpx
import pytest
import respx

from kb import claude
from kb.ingestion.sources import HostGuard
from kb.ingestion.vision import ClaudeImageDescriber

AWS_VARS = (
    "CLAUDE_PROVIDER",
    "CLAUDE_BEDROCK_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_AWS_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AWS_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude, "_aws_credentials", lambda: False)


def test_bedrock_is_the_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")
    assert claude.provider() == "bedrock" and claude.configured()
    client = claude.client(max_retries=1, timeout=5.0)
    assert isinstance(client, anthropic.AsyncAnthropicBedrock)
    assert str(client.base_url) == "https://bedrock-runtime.ap-southeast-2.amazonaws.com"


def test_bedrock_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bare names become inference profiles (global by default, or CLAUDE_BEDROCK_PROFILE).
    assert claude.model_id("claude-opus-4-6") == "global.anthropic.claude-opus-4-6-v1"
    monkeypatch.setenv("CLAUDE_BEDROCK_PROFILE", "au")
    assert claude.model_id("claude-sonnet-4-6") == "au.anthropic.claude-sonnet-4-6"
    assert claude.model_id("claude-haiku-4-5") == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    # A full Bedrock id is sent as it is.
    full = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert claude.model_id(full) == full


def test_prices_and_effort_follow_the_bare_name() -> None:
    from kb.answer.llm import price

    assert claude.base_name("au.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert claude.base_name("au.anthropic.claude-opus-4-6-v1") == "claude-opus-4-6"
    assert claude.base_name("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert price("au.anthropic.claude-sonnet-4-6") == (3.00, 15.00)  # budget caps keep working
    # Haiku 4.5 rejects the effort setting, so it is left out.
    assert claude.output_config("au.anthropic.claude-haiku-4-5-20251001-v1:0", "low") == {}
    assert claude.output_config("claude-opus-4-6", "low") == {"effort": "low"}


def test_bedrock_needs_a_region_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "AWS_REGION" in (claude.missing() or "")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert "credentials" in (claude.missing() or "")
    monkeypatch.setattr(claude, "_aws_credentials", lambda: True)  # e.g. the EC2 role
    assert claude.configured()


def test_anthropic_route_keeps_bare_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
    assert not claude.configured()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert claude.configured() and claude.model_id("claude-opus-4-6") == "claude-opus-4-6"
    assert claude.model_id("au.anthropic.claude-opus-4-6-v1") == "claude-opus-4-6"
    assert isinstance(claude.client(max_retries=1, timeout=5.0), anthropic.AsyncAnthropic)


def test_unknown_provider_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROVIDER", "openai")
    with pytest.raises(ValueError):
        claude.provider()


async def public(host: str) -> list[str]:
    return ["93.184.216.34"]


@respx.mock
async def test_bedrock_screenshots_are_sent_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")
    describer = ClaudeImageDescriber("claude-haiku-4-5")
    describer.guard = HostGuard(resolver=public)
    respx.get("https://cdn.example.com/a.png").mock(
        return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    )
    source = await describer.image_source("https://cdn.example.com/a.png")
    assert source == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(b"\x89PNG").decode(),
    }
    respx.get("https://cdn.example.com/page").mock(
        return_value=httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(ValueError, match="unsupported image type"):
        await describer.image_source("https://cdn.example.com/page")


async def test_bedrock_screenshot_download_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def private(host: str) -> list[str]:
        return ["169.254.169.254"]

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")
    describer = ClaudeImageDescriber("claude-haiku-4-5")
    describer.guard = HostGuard(resolver=private)
    with pytest.raises(ValueError, match="download failed"):
        await describer.image_source("https://metadata.internal/latest")


def test_an_empty_token_line_falls_back_to_aws_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    # `AWS_BEARER_TOKEN_BEDROCK=` left blank in .env must not become an empty bearer token.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    client = claude.client(max_retries=1, timeout=5.0)
    assert isinstance(client, anthropic.AsyncAnthropicBedrock) and client.api_key is None
