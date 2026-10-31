"""Where Claude is reached. Every Claude call (extraction, merging, scoping, the proposal)
gets its client, model id and effort setting from here (same module as kb/claude.py).

`CLAUDE_PROVIDER` picks the route:
- `bedrock` (default): Claude on Amazon Bedrock (`bedrock-runtime`, InvokeModel). Region from
  `AWS_REGION`; credentials from a Bedrock bearer token (`AWS_BEARER_TOKEN_BEDROCK`) or the
  standard AWS chain (access keys, `AWS_PROFILE`/SSO, the EC2 or ECS role).
- `anthropic`: Anthropic's own API with `ANTHROPIC_API_KEY`.

Model settings take either a bare name (`claude-sonnet-4-6`) or a full Bedrock id
(`au.anthropic.claude-sonnet-4-6`). On Bedrock a bare name becomes an inference profile id
with the prefix in `CLAUDE_BEDROCK_PROFILE` (`global` by default; `au`, `us`, `eu`, `jp`,
`apac`). Prices and logs always use the bare name (`base_name`).

The SDK stays below anthropic 2.0: from 2.0 it sends API version 2026-10-21, which Bedrock
rejects for Claude 4.6 and earlier.
"""

import os
import re
from typing import Any, Literal

import anthropic

Provider = Literal["bedrock", "anthropic"]
Client = anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock
BEDROCK_TOKEN_VARS = ("AWS_BEARER_TOKEN_BEDROCK",)

# Bedrock base ids that differ from "anthropic.<bare name>" (ARN-versioned models).
BEDROCK_IDS = {
    "claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
}
# Models that reject `output_config.effort` (a 400): the request leaves it out.
NO_EFFORT = {"claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4", "claude-opus-4-1"}

_PROFILE = re.compile(r"^(global|us|eu|jp|apac|au|ca|us-gov)\.")
_VERSION = re.compile(r"(-\d{8})?(-v\d+(:\d+)?)?$")


def provider() -> Provider:
    value = os.environ.get("CLAUDE_PROVIDER", "bedrock").strip().lower()
    if value not in ("bedrock", "anthropic"):
        raise ValueError(f"CLAUDE_PROVIDER must be 'bedrock' or 'anthropic', not {value!r}")
    return value  # type: ignore[return-value]


def base_name(model: str) -> str:
    """`au.anthropic.claude-haiku-4-5-20251001-v1:0` -> `claude-haiku-4-5`."""
    name = _PROFILE.sub("", model.split("/")[-1]).removeprefix("anthropic.")
    return _VERSION.sub("", name)


def model_id(model: str) -> str:
    """The id to send for a model setting on the current route."""
    if provider() == "anthropic":
        return base_name(model)
    if "anthropic." in model or model.startswith("arn:"):
        return model  # already a Bedrock id or inference profile
    profile = os.environ.get("CLAUDE_BEDROCK_PROFILE", "global").strip()
    base = BEDROCK_IDS.get(model, f"anthropic.{model}")
    return f"{profile}.{base}" if profile else base


def output_config(model: str, effort: str) -> Any:
    """`output_config` for a request: the effort level, unless the model rejects it."""
    return {} if base_name(model) in NO_EFFORT else {"effort": effort}


def client(max_retries: int, timeout: float) -> Client:
    if provider() == "bedrock":
        # An empty token line in .env would be sent as an empty bearer token instead of
        # signing with the AWS credentials, so drop it.
        for name in BEDROCK_TOKEN_VARS:
            if name in os.environ and not os.environ[name].strip():
                del os.environ[name]
        # Same Messages API surface as AsyncAnthropic (parse, caching, structured output).
        return anthropic.AsyncAnthropicBedrock(
            aws_region=bedrock_region(), max_retries=max_retries, timeout=timeout
        )
    return anthropic.AsyncAnthropic(max_retries=max_retries, timeout=timeout)


def bedrock_region() -> str | None:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def configured() -> bool:
    """Whether Claude can be called: features that need it are switched off otherwise."""
    return missing() is None


def missing() -> str | None:
    """What is not configured, in words for a 503, or None when Claude is reachable."""
    if provider() == "anthropic":
        return None if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY is not set"
    if not bedrock_region():
        return "AWS_REGION is not set (Claude runs on Amazon Bedrock)"
    if any(os.environ.get(v, "").strip() for v in BEDROCK_TOKEN_VARS) or _aws_credentials():
        return None
    return "no AWS credentials for Amazon Bedrock (AWS_BEARER_TOKEN_BEDROCK or an AWS role/profile)"


def _aws_credentials() -> bool:
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    try:
        import botocore.session  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        return botocore.session.get_session().get_credentials() is not None
    except Exception:  # a broken profile is "not configured", not a crash at start-up
        return False
