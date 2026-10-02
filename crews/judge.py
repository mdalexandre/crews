"""TypeSafe judge: typed semantic answers for the budget (request 1) and for each role (request 2).

Interface
    request1(cat, task) -> body          the prototype's four questions over {"task": text}
    request2(cat, task, roles) -> body   need.<i>, difficulty.<i>, divisible.<i>, specific.<i> over {"task", "roles"}
    parse1(cat, response) -> answers     raises JudgeParseError naming the field
    parse2(cat, response, n) -> [role answers]
    parse2_confidence(cat, response, n) -> [role answers, need carries confidence + probabilities]
                                          same request 2 response, read a second way for JEV-11/JEV-12: the
                                          need answer's confidence and probability distribution ride along
                                          instead of being dropped. Confidence/probabilities are None when the
                                          response omits them, never invented or defaulted; a malformed value
                                          still raises JudgeParseError. parse2 itself is untouched, so no
                                          existing caller sees a different shape.
    call(body) -> response               live HTTP; raises JudgeUnavailable with a reason that never holds the key

Only call() touches the environment or the network, and only when a live judge was asked for.
The key comes from TYPESAFE_API_KEY, else from the file named by TYPESAFE_API_KEY_FILE. It is sent in the
Authorization header and nowhere else: never printed, logged, stored, or put in an exception message.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT_S = 10.0
RETRIES = 2
RETRY_AFTER_CAP_S = 5.0
LEVELS_MAX = 3.0
BODY_READ_CAP_BYTES = 4096
REASON_CHAR_CAP = 200
_sleep: Callable[[float], None] = time.sleep


class JudgeUnavailable(RuntimeError):
    """The judge could not answer. The caller falls back; the reason holds no secret."""


class JudgeParseError(ValueError):
    """A response that does not match its question. The message names the field."""


def _role_state(roles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in roles:
        row: dict[str, Any] = {"name": r.get("name"), "kind": r.get("kind"), "mission": r.get("mission"),
                               "scope": r.get("scope") or []}
        frame = r.get("professional_frame")
        if frame:
            row["professional_frame"] = frame
        out.append(row)
    return out


def request1(cat: dict[str, Any], task: str) -> dict[str, Any]:
    return {"model": MODEL, "state": {"task": task}, "questions": cat["questions"]}


def request2(cat: dict[str, Any], task: str, roles: list[dict[str, Any]]) -> dict[str, Any]:
    q = cat["questions"]
    questions: dict[str, Any] = {}
    for i in range(len(roles)):
        ref = f"`roles[{i}]`"
        questions[f"need.{i}"] = {"type": "choice", "criteria": q["need"]["criteria"],
                                  "instructions": f"Which kind of work does role {ref} do for `task`?"}
        hard = f"How hard is the work of role {ref} for a capable coding agent?"
        questions[f"difficulty.{i}"] = {"type": "score", "instructions": hard, "criteria": q["difficulty"]["criteria"]}
        questions[f"divisible.{i}"] = {"type": "noul", "instructions": (
            f"Can the work of role {ref} be split into parts that separate workers could do at the same time "
            "without talking to each other?")}
        questions[f"specific.{i}"] = {"type": "noul", "instructions": (
            f"Does role {ref} name a concrete target in `task`, rather than a function any task could have?")}
    return {"model": MODEL, "state": {"task": task, "roles": _role_state(roles)}, "questions": questions}


def _prob(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise JudgeParseError(f"{field}: must be a probability from 0 to 1")
    return float(value)


def _answers(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        raise JudgeParseError("answers: missing")
    out: dict[str, Any] = response["answers"]
    return out


def _choice(cat: dict[str, Any], ans: dict[str, Any], qid: str) -> dict[str, Any]:
    a = ans.get(qid)
    if not isinstance(a, dict):
        raise JudgeParseError(f"{qid}: missing answer")
    if a.get("choice") not in cat["needs"]:
        raise JudgeParseError(f"{qid}.choice: unknown need {a.get('choice')!r}")
    probs = a.get("probabilities") or {}
    if not isinstance(probs, dict):
        raise JudgeParseError(f"{qid}.probabilities: must be an object")
    clean: dict[str, float] = {}
    for k, v in probs.items():
        if k not in cat["needs"]:
            raise JudgeParseError(f"{qid}.probabilities: unknown need {k!r}")
        clean[k] = _prob(v, f"{qid}.probabilities.{k}")
    return {"choice": a["choice"], "confidence": _prob(a.get("confidence"), f"{qid}.confidence"),
            "probabilities": clean}


def _noul(ans: dict[str, Any], qid: str) -> float:
    a = ans.get(qid)
    if not isinstance(a, dict):
        raise JudgeParseError(f"{qid}: missing answer")
    return _prob(a.get("noul"), f"{qid}.noul")


def _score(ans: dict[str, Any], qid: str) -> float:
    a = ans.get(qid)
    if not isinstance(a, dict):
        raise JudgeParseError(f"{qid}: missing answer")
    s = a.get("score")
    if isinstance(s, bool) or not isinstance(s, (int, float)) or not 0 <= s <= LEVELS_MAX:
        raise JudgeParseError(f"{qid}.score: must be a number from 0 to 3")
    return float(s)


def parse1(cat: dict[str, Any], response: Any) -> dict[str, Any]:
    ans = _answers(response)
    return {"need": _choice(cat, ans, "need"), "divisible": {"noul": _noul(ans, "divisible")},
            "needs_verifier": {"noul": _noul(ans, "needs_verifier")},
            "difficulty": {"score": _score(ans, "difficulty")}}


def parse2(cat: dict[str, Any], response: Any, n: int) -> list[dict[str, Any]]:
    ans = _answers(response)
    return [{"need": _choice(cat, ans, f"need.{i}")["choice"], "difficulty": _score(ans, f"difficulty.{i}"),
             "divisible": _noul(ans, f"divisible.{i}"), "specific": _noul(ans, f"specific.{i}")} for i in range(n)]


def _soft_confidence(a: dict[str, Any], qid: str) -> float | None:
    """The need answer's confidence, or None when the response never carried one. Never invented,
    never defaulted to 0 or 1: a caller measuring a role level band must be able to tell absence
    from a real low-confidence answer."""
    if "confidence" not in a:
        return None
    return _prob(a["confidence"], f"{qid}.confidence")


def _soft_probabilities(cat: dict[str, Any], a: dict[str, Any], qid: str) -> dict[str, float] | None:
    """The need answer's probability distribution, or None when the response never carried one."""
    if a.get("probabilities") is None:
        return None
    probs = a["probabilities"]
    if not isinstance(probs, dict):
        raise JudgeParseError(f"{qid}.probabilities: must be an object")
    clean: dict[str, float] = {}
    for k, v in probs.items():
        if k not in cat["needs"]:
            raise JudgeParseError(f"{qid}.probabilities: unknown need {k!r}")
        clean[k] = _prob(v, f"{qid}.probabilities.{k}")
    return clean


def _need_with_confidence(cat: dict[str, Any], ans: dict[str, Any], qid: str) -> dict[str, Any]:
    a = ans.get(qid)
    if not isinstance(a, dict):
        raise JudgeParseError(f"{qid}: missing answer")
    if a.get("choice") not in cat["needs"]:
        raise JudgeParseError(f"{qid}.choice: unknown need {a.get('choice')!r}")
    return {"choice": a["choice"], "confidence": _soft_confidence(a, qid),
            "probabilities": _soft_probabilities(cat, a, qid)}


def parse2_confidence(cat: dict[str, Any], response: Any, n: int) -> list[dict[str, Any]]:
    """Per role answers for request 2, same as parse2 but with `need` expanded to
    {"choice", "confidence", "probabilities"} so a role level band (JEV-11, JEV-12) can later be
    measured over (confidence, correct) pairs. `confidence` and `probabilities` are carried straight
    from the response: None when the response omits them, a JudgeParseError naming the field when
    the value present is malformed. Does not change parse2 or any of its callers."""
    ans = _answers(response)
    return [{"need": _need_with_confidence(cat, ans, f"need.{i}"), "difficulty": _score(ans, f"difficulty.{i}"),
             "divisible": _noul(ans, f"divisible.{i}"), "specific": _noul(ans, f"specific.{i}")} for i in range(n)]


def meta(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    return {"model": response.get("model"), "usage": response.get("usage")}


def _key() -> str:
    direct = os.environ.get("TYPESAFE_API_KEY")
    if direct:
        return direct.strip()
    path = os.environ.get("TYPESAFE_API_KEY_FILE")
    if path:
        try:
            value = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise JudgeUnavailable(f"key file unreadable ({type(exc).__name__})") from None
        if value:
            return value
    raise JudgeUnavailable("no key: set TYPESAFE_API_KEY_FILE to the key file's path")


def _scrub(value: Any, key: str) -> Any:
    """Replace the key wherever an endpoint echoed it, so no answer, note, or meta field can carry it onward."""
    if isinstance(value, str):
        return value.replace(key, "[redacted]") if key else value
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    if isinstance(value, dict):
        return {_scrub(k, key): _scrub(v, key) for k, v in value.items()}
    return value


def _clamp(text: str) -> str:
    """Collapse whitespace to one line and cut to REASON_CHAR_CAP characters."""
    return " ".join(text.split())[:REASON_CHAR_CAP]


def _safe(text: str, key: str) -> str:
    """Normalize first, redact last. `_clamp` collapses whitespace, so running it after `_scrub` lets a
    key split across newlines pass the scrub unmatched and then be rejoined into the literal credential.
    That ordering is CWE-180 (incorrect behavior order: validate before canonicalize), and the invariant
    it violates is that the final scrub must see the exact representation that reaches the sink. So every
    branch of `_reason_from_body` returns through here, and nothing may transform the string afterward.
    Deliberately not a decode cascade: chaining speculative decoders to hunt fragments is CWE-174 (double
    decoding), which buys new representations rather than fewer. `_scrub` stays the one redaction routine
    (JEV-04); the isinstance guard only restores the str type its Any signature erases."""
    redacted = _scrub(_clamp(text), key)
    return redacted if isinstance(redacted, str) else ""


# The top level names an error body may carry the reason under, and the names it nests that reason
# beneath one level down. TypeSafe's OpenAPI declares only 200 and 422 for POST /v1/systemone, so there
# is no published schema for the 401, 403 and 429 we are most likely to see. What there is: 422 is
# documented as detail: [{loc, msg, type}], a live probe recorded 401 and 403 as detail: {error_type,
# message}, and the official SDKs read top level error, message and detail plus error.message and
# detail.message. These two tuples are the union of those, which is why a plain string alone is not
# enough: on the observed 403 shape, a top-level-string-only read returns nothing and the operator gets
# a bare "HTTP 403" carrying no reason at all.
_REASON_FIELDS = ("error", "message", "detail", "code")
_NESTED_FIELDS = ("message", "msg", "error_type", "detail", "error")
_LIST_SCAN_CAP = 3
_NEST_DEPTH = 2


def _first_text(value: Any, depth: int = _NEST_DEPTH) -> str:
    """Pull the one human readable string out of an error field across those three shapes. Descends at
    most `depth` levels and reads only the names above, never walking the body openly: an error body is
    untrusted input, and an unbounded walk is how a key echoed into an unexpected corner finds its way
    out. The depth of 2 is what the list-of-objects 422 shape needs (list, then object, then string) and
    is deliberately no deeper. Returns "" for anything blank, absent, or shaped otherwise."""
    if isinstance(value, str):
        return value if value.strip() else ""
    if depth <= 0:
        return ""
    if isinstance(value, dict):
        for name in _NESTED_FIELDS:
            found = _first_text(value.get(name), depth - 1)
            if found:
                return found
        return ""
    if isinstance(value, list):
        for item in value[:_LIST_SCAN_CAP]:
            found = _first_text(item, depth - 1)
            if found:
                return found
    return ""


def _reason_from_body(exc: urllib.error.HTTPError, key: str) -> str:
    """Read up to BODY_READ_CAP_BYTES of the error body and try to name why the call failed, scrubbed
    of the key through `_scrub` (JEV-04: no second redaction routine). Never raises: an already
    consumed stream, an empty body, invalid UTF-8, non JSON HTML, or a body far over the cap all fall
    through to the empty string, which call() turns into the plain `HTTP {code}` message."""
    try:
        raw = exc.read(BODY_READ_CAP_BYTES)
    except Exception:
        return ""
    if not raw:
        return ""
    if len(raw) >= BODY_READ_CAP_BYTES:
        # Read hit the cap: the body was far larger than we ever accept, so the bytes we hold may be
        # cut mid character, mid JSON token, or mid key, which could defeat _scrub's substring match
        # on a partial key. Discard rather than guess at a truncated reason.
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        # Non JSON body. An HTML error page (a proxy or gateway's own 502/403 page) carries nothing an
        # operator can use, so it is treated the same as an empty body. Any other non JSON text falls
        # back to a scrubbed, truncated single line snippet, per the fallback rule above.
        if text.strip().startswith("<"):
            return ""
        return _safe(text, key)
    if not isinstance(parsed, dict):
        return ""
    # Selected from `parsed` rather than from a pre-scrubbed copy so that `_safe` performs the redaction
    # last, on the exact string being returned. Only the named fields below are ever read, so a key
    # echoed into a dict key, or into any corner these names do not reach, has no path out of here.
    for field in _REASON_FIELDS:
        text = _first_text(parsed.get(field))
        if text:
            return _safe(text, key)
    return ""


def call(body: dict[str, Any], sleep: Callable[[float], None] | None = None, retries: int = RETRIES) -> dict[str, Any]:
    """POST the body. Honors retry-after on 429 and 529 up to `retries` retries (default RETRIES, the Phase 5
    behavior). Pass retries=0 for the egress gate's one-shot path: a single approval authorizes a single
    network attempt, never an automatic retry. Never raises anything but JudgeUnavailable, and its message
    never carries the key."""
    key = _key()
    url = os.environ.get("CREW_TYPESAFE_URL") or URL
    timeout = float(os.environ.get("CREW_TYPESAFE_TIMEOUT") or TIMEOUT_S)
    data = json.dumps(body).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                parsed: dict[str, Any] = _scrub(json.loads(resp.read().decode("utf-8")), key)
                return parsed
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (429, 529) and attempt < retries:
                attempt += 1
                try:
                    wait = min(float(exc.headers.get("retry-after") or 1.0), RETRY_AFTER_CAP_S)
                except ValueError:
                    wait = 1.0
                (sleep or _sleep)(max(wait, 0.0))
                continue
            # The retry decision above used only exc.headers; the body is read here, once, after that
            # decision is final, so a 429/529 still eligible for retry never touches the (single read)
            # HTTPError stream the retry path does not need. reason is "" when the body carried nothing
            # usable, in which case the message is exactly the old plain "HTTP {code}[ after retries]".
            reason = _reason_from_body(exc, key)
            # The only field TypeSafe support can trace a specific rejection by, and the one thing a 403
            # here is otherwise undiagnosable without. Vendor-controlled input, so it goes through _safe
            # like any body text rather than being trusted; absent header, unreadable headers, and a
            # test double that carries none all collapse to "" and the message is unchanged.
            try:
                request_id = _safe(exc.headers.get("x-typesafe-request-id") or "", key)
            except Exception:
                request_id = ""
            message = f"HTTP {code}" + (" after retries" if code in (429, 529) else "")
            if reason:
                message += f": {reason}"
            if request_id:
                message += f" [request-id {request_id}]"
            raise JudgeUnavailable(message) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise JudgeUnavailable(f"network: {type(exc).__name__}") from None
        except (ValueError, UnicodeDecodeError):
            raise JudgeUnavailable("response is not JSON") from None
