"""Call the logged-in Grok reviewer on one assembled prompt.

The prompt is the rubric prefix plus the diff. The key is not sent.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

# This account's `grok models` list has no grok-4-fast id. grok-4.7-build-fast
# is the fast Grok model the login can run. A user diff still uses this model.
REVIEWER_MODEL = "grok-4.7-build-fast"
# Sonnet rerun of the stored stress test. Not the default for a user diff.
SONNET_MODEL = "claude-sonnet-5-5"
# Haiku 4.5 stress run. Not the default for a user diff, and not the Sonnet id.
HAIKU_MODEL = "claude-haiku-4-5-20251001"

_FINDINGS_SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["file", "line", "what_breaks", "fix"],
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "what_breaks": {"type": "string"},
                        "fix": {"type": "string"},
                    },
                },
            }
        },
    },
    separators=(",", ":"),
)


class ProviderError(RuntimeError):
    pass


class CredentialError(ProviderError):
    """The reviewer CLI has no usable login. The stress run must not invent a recall."""


def _credential_error(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "failed to authenticate",
            "oauth",
            "not logged in",
            "no credential",
            "api key",
            "authentication",
            "weekly limit",
            "rate limit",
        )
    )


def parse_reviewer_stdout(stdout: str) -> object:
    """Pull findings out of the CLI's JSON envelope."""
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError("reviewer returned non-json") from exc
    if not isinstance(body, dict):
        raise ProviderError("reviewer returned a non-object")
    structured = body.get("structuredOutput")
    if isinstance(structured, dict):
        return structured
    text = body.get("text")
    if isinstance(text, str) and text.strip():
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("reviewer text was not findings json") from exc
    raise ProviderError("reviewer returned no findings")


def claude_command(model: str = SONNET_MODEL) -> list[str]:
    """Non-interactive Claude CLI argv. Tools are disabled. The prompt is stdin."""
    return [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--tools",
        "",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--safe-mode",
        "--system-prompt",
        "Follow the user message exactly.",
        "--json-schema",
        _FINDINGS_SCHEMA,
    ]


def parse_claude_stdout(stdout: str) -> object:
    """Pull findings out of `claude -p --output-format json`."""
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError("claude returned non-json") from exc
    if not isinstance(body, dict):
        raise ProviderError("claude returned a non-object")
    if body.get("is_error"):
        message = str(body.get("result") or body.get("terminal_reason") or "claude failed")
        if _credential_error(message):
            raise CredentialError(message)
        raise ProviderError(message)
    for key in ("structured_output", "structuredOutput"):
        structured = body.get(key)
        if isinstance(structured, dict):
            return structured
    text = body.get("result")
    if isinstance(text, str) and text.strip():
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("claude result was not findings json") from exc
    raise ProviderError("claude returned no findings")


def review_diff_claude(
    prompt: str, *, model: str = SONNET_MODEL, timeout_s: int = 300
) -> object:
    """Send prefix-plus-diff through the Claude CLI. Defaults to Sonnet."""
    if not isinstance(prompt, str) or prompt == "":
        raise ProviderError("empty prompt")
    try:
        proc = subprocess.run(
            claude_command(model),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProviderError("claude cli is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"claude timed out after {timeout_s}s") from exc
    stdout = proc.stdout or ""
    if stdout.strip().startswith("{"):
        payload = parse_claude_stdout(stdout)
        if proc.returncode != 0:
            detail = (proc.stderr or stdout or "claude failed").strip()
            if _credential_error(detail):
                raise CredentialError(detail)
            raise ProviderError(detail)
        return payload
    detail = (proc.stderr or stdout or "claude failed").strip()
    if _credential_error(detail):
        raise CredentialError(detail)
    if proc.returncode != 0:
        raise ProviderError(detail or "claude failed")
    return parse_claude_stdout(stdout)


def review_diff(
    prompt: str,
    *,
    model: str = REVIEWER_MODEL,
    timeout_s: int = 300,
    attempt_usages: list | None = None,
) -> object:
    """Send prefix-plus-diff to the reviewer and return the findings object.

    One retry covers a transient non-json reply. The prompt is unchanged.
    Each provider call appends an AttemptUsage to attempt_usages when given,
    including failed attempts whose cost we absorb.
    """
    if not isinstance(prompt, str) or prompt == "":
        raise ProviderError("empty prompt")
    last: ProviderError | None = None
    for _attempt in range(2):
        try:
            payload, usage = _review_diff_once(prompt, model=model, timeout_s=timeout_s)
            if attempt_usages is not None:
                attempt_usages.append(usage)
            return payload
        except CredentialError:
            raise
        except ProviderError as exc:
            if attempt_usages is not None:
                attempt_usages.append(
                    _attempt_usage(model, input_tokens=0, output_tokens=0)
                )
            last = exc
    assert last is not None
    raise last


def _attempt_usage(model: str, *, input_tokens: int, output_tokens: int):
    from review_grade.api.review_cost import AttemptUsage

    return AttemptUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _usage_from_cli_body(body: object, *, model: str):
    """Pull token counts from a CLI JSON envelope when the provider reports them."""
    if not isinstance(body, dict):
        return _attempt_usage(model, input_tokens=0, output_tokens=0)
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return _attempt_usage(model, input_tokens=0, output_tokens=0)
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    try:
        return _attempt_usage(
            model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
        )
    except (TypeError, ValueError):
        return _attempt_usage(model, input_tokens=0, output_tokens=0)


def _review_diff_once(prompt: str, *, model: str, timeout_s: int) -> tuple[object, object]:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".txt", delete=False
    )
    path = handle.name
    try:
        handle.write(prompt)
        handle.close()
        proc = subprocess.run(
            [
                "grok",
                "--prompt-file",
                path,
                "--verbatim",
                "-m",
                model,
                "--max-turns",
                "1",
                "--output-format",
                "json",
                "--disable-web-search",
                "--no-plan",
                "--no-subagents",
                "--tools",
                "",
                "--system-prompt-override",
                "Follow the user message exactly.",
                "--json-schema",
                _FINDINGS_SCHEMA,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"reviewer timed out after {timeout_s}s") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "reviewer failed").strip()
        raise ProviderError(detail)
    usage = _attempt_usage(model, input_tokens=0, output_tokens=0)
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        envelope = None
    else:
        usage = _usage_from_cli_body(envelope, model=model)
    return parse_reviewer_stdout(proc.stdout), usage
