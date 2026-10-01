"""Call the logged-in Grok reviewer on one assembled prompt.

The prompt is the rubric prefix plus the diff. The key is not sent.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

# This account's `grok models` list has no grok-4-fast id. grok-4.7-build-fast
# is the fast Grok model the login can run.
REVIEWER_MODEL = "grok-4.7-build-fast"

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


def review_diff(prompt: str, *, model: str = REVIEWER_MODEL, timeout_s: int = 300) -> object:
    """Send prefix-plus-diff to the reviewer and return the findings object."""
    if not isinstance(prompt, str) or prompt == "":
        raise ProviderError("empty prompt")
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
                "-m",
                model,
                "--max-turns",
                "1",
                "--output-format",
                "json",
                "--disable-web-search",
                "--no-plan",
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
    return parse_reviewer_stdout(proc.stdout)
