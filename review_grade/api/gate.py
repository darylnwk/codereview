"""Accept or refuse a review without touching sqlite or the socket."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

WINDOW_SECONDS = 60
ACCEPT_LIMIT = 10
_LINE_CAP = 8000
_LISTED_MAX = 20000
_SOURCES = frozenset({"web", "api", "ci", "agent"})
_LOCKFILES = frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock"})
_GENERATED_SEGMENTS = frozenset({"snapshots", "vendor", "vendored"})

_REFUSALS = {
    "bad_request": (400, "The review request is not valid."),
    "unauthorized": (401, "The API key is not valid."),
    "no_credit": (402, "The account has no credits."),
    "not_found": (404, "The run was not found."),
    "conflict": (409, "The idempotency key was already used for a different diff."),
    "too_large": (413, "The diff is too large to review."),
    "rate_limited": (429, "Too many reviews were accepted in the last minute."),
}


@dataclass(frozen=True)
class PostOp:
    key_ok: bool
    credits: int
    payload: object
    idempotency_key: str | None
    prior_run_id: str | None
    prior_diff_hash: str | None
    accepts_in_window: int


@dataclass(frozen=True)
class GetRunOp:
    key_ok: bool
    visible: bool


@dataclass(frozen=True)
class GetMeOp:
    key_ok: bool


@dataclass(frozen=True)
class Accepted:
    status: int
    kept_diff: str
    diff_hash: str
    source: str


@dataclass(frozen=True)
class Replay:
    status: int
    run_id: str


@dataclass(frozen=True)
class ShowRun:
    status: int


@dataclass(frozen=True)
class ShowCredits:
    status: int


@dataclass(frozen=True)
class Refused:
    code: str
    status: int
    message: str
    files: tuple[str, ...] | None = None


Decision = Accepted | Replay | ShowRun | ShowCredits | Refused


def refuse(code: str, files: tuple[str, ...] | None = None) -> Refused:
    status, message = _REFUSALS[code]
    return Refused(code, status, message, files)


def decide(op: PostOp | GetRunOp | GetMeOp) -> Decision:
    if isinstance(op, PostOp):
        return _decide_post(op)
    if isinstance(op, GetRunOp):
        return _decide_get_run(op)
    if isinstance(op, GetMeOp):
        return _decide_get_me(op)
    raise TypeError(type(op))


def _decide_get_run(op: GetRunOp) -> Decision:
    if not op.key_ok:
        return refuse("unauthorized")
    if not op.visible:
        return refuse("not_found")
    return ShowRun(200)


def _decide_get_me(op: GetMeOp) -> Decision:
    if not op.key_ok:
        return refuse("unauthorized")
    return ShowCredits(200)


def _decide_post(op: PostOp) -> Decision:
    if not op.key_ok:
        return refuse("unauthorized")
    parsed = _parse_payload(op.payload)
    if isinstance(parsed, Refused):
        return parsed
    raw, source, files = parsed
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if op.idempotency_key is not None and op.prior_run_id is not None:
        if op.prior_diff_hash == digest:
            return Replay(202, op.prior_run_id)
        return refuse("conflict")
    if op.credits <= 0:
        return refuse("no_credit")
    kept = _select(raw, files)
    if kept.strip() == "":
        return refuse("bad_request")
    sized = _size(kept)
    if sized is not None:
        return sized
    if op.accepts_in_window >= ACCEPT_LIMIT:
        return refuse("rate_limited")
    return Accepted(202, kept, digest, source)


def _parse_payload(
    payload: object,
) -> tuple[str, str, tuple[str, ...] | None] | Refused:
    if not isinstance(payload, dict):
        return refuse("bad_request")
    diff = payload.get("diff")
    if not isinstance(diff, str) or diff.strip() == "":
        return refuse("bad_request")
    if "source" not in payload:
        source = "api"
    else:
        source = payload["source"]
        if not isinstance(source, str) or source not in _SOURCES:
            return refuse("bad_request")
    if "files" not in payload:
        files = None
    else:
        raw_files = payload["files"]
        if not isinstance(raw_files, list) or any(not isinstance(item, str) for item in raw_files):
            return refuse("bad_request")
        files = tuple(raw_files)
    return diff, source, files


def _size(diff: str) -> Refused | None:
    paths = _paths(diff)
    count = _changed_line_count(diff)
    generated_only = bool(paths) and all(_generated(path) for path in paths)
    if generated_only and count < _LINE_CAP:
        return refuse("too_large")
    if count < _LINE_CAP:
        return None
    if count <= _LISTED_MAX:
        return refuse("too_large", files=paths)
    return refuse("too_large")


def _changed_line_count(diff: str) -> int:
    count = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def _select(diff: str, files: tuple[str, ...] | None) -> str:
    if files is None:
        return diff
    wanted = set(files)
    return "".join(text for path, text in _sections(diff) if path in wanted)


def _paths(diff: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path, _text in _sections(diff):
        if path is None or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return tuple(ordered)


def _sections(diff: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    bucket: list[str] = []
    path: str | None = None
    started = False
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if started or bucket:
                sections.append((path, "".join(bucket)))
            bucket = [line]
            path = _path_from_header(line)
            started = True
        else:
            bucket.append(line)
    if bucket:
        sections.append((path, "".join(bucket)))
    return sections


def _path_from_header(line: str) -> str | None:
    text = line.rstrip("\r\n")
    marker = "diff --git "
    if not text.startswith(marker):
        return None
    pair = _split_paths(text[len(marker) :])
    if pair is None:
        return None
    old, new = pair
    if _null_path(new) and not _null_path(old):
        return old
    if _null_path(new):
        return None
    return new


def _null_path(path: str) -> bool:
    return path in {"/dev/null", "dev/null"}


def _split_paths(rest: str) -> tuple[str, str] | None:
    try:
        old_token, index = _one_token(rest, 0)
        if index < len(rest) and rest[index] == " ":
            index += 1
        new_token, _end = _one_token(rest, index)
    except ValueError:
        return None
    return _strip_side(old_token, "a"), _strip_side(new_token, "b")


def _strip_side(token: str, side: str) -> str:
    prefix = side + "/"
    if token.startswith(prefix):
        return token[len(prefix) :]
    return token


def _one_token(text: str, index: int) -> tuple[str, int]:
    if index >= len(text):
        raise ValueError
    if text[index] != '"':
        end = text.find(" ", index)
        if end == -1:
            return text[index:], len(text)
        return text[index:end], end
    chars: list[str] = []
    cursor = index + 1
    while cursor < len(text):
        char = text[cursor]
        if char == '"':
            return "".join(chars), cursor + 1
        if char != "\\":
            chars.append(char)
            cursor += 1
            continue
        if cursor + 1 >= len(text):
            raise ValueError
        nxt = text[cursor + 1]
        mapped = {
            "n": "\n",
            "t": "\t",
            "\\": "\\",
            '"': '"',
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "r": "\r",
            "v": "\v",
        }
        if nxt in mapped:
            chars.append(mapped[nxt])
            cursor += 2
            continue
        if nxt.isdigit():
            octal = nxt
            cursor += 2
            for _ in range(2):
                if cursor < len(text) and text[cursor].isdigit():
                    octal += text[cursor]
                    cursor += 1
                else:
                    break
            chars.append(chr(int(octal, 8)))
            continue
        chars.append(nxt)
        cursor += 2
    raise ValueError


def _generated(path: str) -> bool:
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts:
        return False
    if parts[-1] in _LOCKFILES:
        return True
    return any(part in _GENERATED_SEGMENTS for part in parts)
