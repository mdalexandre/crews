"""crews/egress.py, the owner approval egress ledger (SRD typesafe-integration-v1.0 section 2.5, C-EGR).

Interface (exact names and signatures bind the mission contract)
    fingerprint(body) -> str
    packet(body, *, purpose, requirement_ids, data_category, includes_worker_output, decline_behavior) -> dict
    register(pkt) -> None
    approve(call_id, fingerprint, *, batch_id=None) -> None
    approve_batch(manifest_path, manifest_sha256) -> None
    gated_call(body, *, purpose, requirement_ids, data_category, includes_worker_output, decline_behavior,
               transport=None) -> dict
    build_queue_manifest(*, batch_id=None, retention_days=DEFAULT_QUEUE_RETENTION_DAYS,
                          cap=DEFAULT_QUEUE_CAP, now=None) -> dict     (JEV-20/JEV-28, Policy C)

No live TypeSafe request leaves this machine unless the ledger under CREWS_EGRESS_DIR (default
<crews home>/egress) holds a single use, unconsumed owner approval for the exact call id and payload
fingerprint. `gated_call` is the only path that may reach the network (default transport is
`judge.call(body, retries=0)`), and it consumes the approval record before invoking transport exactly
once: no function in this module ever retries, and none reads or stores the TypeSafe key.

Every read modify write against the three ledger files (pending.jsonl, approvals.jsonl, consumed.jsonl,
one JSON object per line) is wrapped in an exclusive fcntl.flock on ledger.lock, so two concurrent callers
who race for the same approval produce exactly one consumption and exactly one transport call.

Policy C (owner OG-04, 2026-09-22, docs/srd/typesafe-jev-activation-v1.0.md JEV-20/JEV-28): routine work
may accumulate pending call intents into a manifest automatically, but no live call executes until the
owner approves that exact manifest sha256. `build_queue_manifest` is the accumulation step: it reads the
open pending queue (registered, not yet approved, not yet consumed), excludes any intent past
`retention_days` so a stale development intent never rides along inside a batch the owner did not know it
was authorizing, refuses outright rather than truncating when the eligible count exceeds `cap`, and
returns a manifest carrying only call ids and fingerprints (opaque, hashed identifiers) alongside a
human-readable summary. It never calls `judge.call`, `gated_call`, or any transport; it only reads the
three ledger files under a shared read (no lock is required: it never mutates the ledger)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from crews import judge, paths

_PENDING = "pending.jsonl"
_APPROVALS = "approvals.jsonl"
_CONSUMED = "consumed.jsonl"
_LOCK = "ledger.lock"


def _dir() -> Path:
    raw = os.environ.get("CREWS_EGRESS_DIR")
    d = Path(raw).expanduser() if raw else paths.state_dir("egress")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(name: str) -> Path:
    return _dir() / name


def _flock(fd: int, *, exclusive: bool) -> None:
    """fcntl.flock where fcntl exists (Linux, macOS); on a platform without it, msvcrt byte locking."""
    try:
        import fcntl
    except ImportError:
        import msvcrt  # type: ignore[import-not-found,unused-ignore]

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if exclusive else msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined,unused-ignore]
        return
    fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)


@contextmanager
def _locked() -> Iterator[None]:
    fh = open(_path(_LOCK), "a+", encoding="utf-8")
    try:
        _flock(fh.fileno(), exclusive=True)
        yield
    finally:
        _flock(fh.fileno(), exclusive=False)
        fh.close()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(body: dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _describe_questions(questions: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, q in questions.items():
        qtype = q.get("type") if isinstance(q, dict) else None
        options: list[Any] = []
        if isinstance(q, dict):
            criteria = q.get("criteria")
            if qtype == "choice" and isinstance(criteria, dict):
                options = sorted(criteria.keys())
            elif qtype == "score" and isinstance(criteria, list):
                options = list(criteria)
        out[qid] = {"type": qtype, "instructions": q.get("instructions") if isinstance(q, dict) else None,
                    "options": options}
    return out


def _next_attempt(fp: str) -> int:
    consumed = _read_jsonl(_path(_CONSUMED))
    return 1 + sum(1 for r in consumed if r.get("fingerprint") == fp)


def packet(body: dict[str, Any], *, purpose: str, requirement_ids: Iterable[str], data_category: str,
           includes_worker_output: bool, decline_behavior: str) -> dict[str, Any]:
    fp = fingerprint(body)
    attempt = _next_attempt(fp)
    call_id = f"ts-{fp[:16]}-{attempt}"
    return {
        "call_id": call_id,
        "attempt": attempt,
        "fingerprint": fp,
        "purpose": purpose,
        "requirement_ids": list(requirement_ids),
        "questions": _describe_questions(body.get("questions") or {}),
        "data_category": data_category,
        "includes_worker_output": bool(includes_worker_output),
        "key_shown": False,
        "decline_behavior": decline_behavior,
        "approve_command": f"crew approve {call_id} --fingerprint {fp}",
    }


def register(pkt: dict[str, Any]) -> None:
    with _locked():
        pending = _read_jsonl(_path(_PENDING))
        if any(r.get("call_id") == pkt["call_id"] for r in pending):
            return
        # Policy C (JEV-20/JEV-28): a registration timestamp so build_queue_manifest can measure an
        # intent's age and exclude one past retention. Added here, once, at the moment the intent
        # actually enters the ledger, rather than inside packet() (a pure function callers may invoke
        # more than once for the same body without ever registering it).
        record = pkt if "registered_at" in pkt else {**pkt, "registered_at": _now()}
        _append_jsonl(_path(_PENDING), record)


def _check_approvable(pending: list[dict[str, Any]], approvals: list[dict[str, Any]],
                       consumed: list[dict[str, Any]], call_id: str, fp: str) -> None:
    match = next((r for r in pending if r.get("call_id") == call_id), None)
    if match is None or match.get("fingerprint") != fp:
        raise ValueError(f"no pending call {call_id} with that fingerprint")
    if any(r.get("call_id") == call_id for r in approvals):
        raise ValueError(f"{call_id} is already approved")
    if any(r.get("call_id") == call_id for r in consumed):
        raise ValueError(f"{call_id} is already consumed")


def approve(call_id: str, fingerprint: str, *, batch_id: str | None = None) -> None:
    with _locked():
        pending = _read_jsonl(_path(_PENDING))
        approvals = _read_jsonl(_path(_APPROVALS))
        consumed = _read_jsonl(_path(_CONSUMED))
        _check_approvable(pending, approvals, consumed, call_id, fingerprint)
        _append_jsonl(_path(_APPROVALS), {"call_id": call_id, "fingerprint": fingerprint,
                                          "batch_id": batch_id, "approved_at": _now()})


def approve_batch(manifest_path: str | Path, manifest_sha256: str) -> None:
    raw = Path(manifest_path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != manifest_sha256:
        raise ValueError("manifest sha256 mismatch")
    manifest = json.loads(raw.decode("utf-8"))
    batch_id = manifest.get("batch_id")
    calls = manifest.get("calls") or []
    with _locked():
        pending = _read_jsonl(_path(_PENDING))
        approvals = _read_jsonl(_path(_APPROVALS))
        consumed = _read_jsonl(_path(_CONSUMED))
        for c in calls:
            _check_approvable(pending, approvals, consumed, c["call_id"], c["fingerprint"])
        for c in calls:
            _append_jsonl(_path(_APPROVALS), {"call_id": c["call_id"], "fingerprint": c["fingerprint"],
                                              "batch_id": batch_id, "approved_at": _now()})


# Policy C queue-to-manifest defaults (JEV-20/JEV-28). Both are owner overridable per call, never
# hardcoded past the CLI: `crew queue-manifest --retention-days ... --cap ...`.
#   retention_days=14: two weeks covers a normal work cycle (the mission's own stale intents were
#   "several... from earlier work", i.e. left over from a prior development phase, not a same-day
#   backlog); anything older is presumptively abandoned rather than merely unreviewed, and Policy C
#   requires it be excluded rather than silently carried into a batch the owner did not ask for.
#   cap=200: a batch the owner can actually read one call/fingerprint pair at a time in one sitting;
#   large enough that ordinary routine accumulation never hits it, small enough that a runaway queue
#   refuses loudly (A5) instead of quietly growing past what "the owner knew what was in it" can mean.
DEFAULT_QUEUE_RETENTION_DAYS = 14.0
DEFAULT_QUEUE_CAP = 200


class QueueManifestRefused(ValueError):
    """The eligible pending queue exceeds `cap`, or the ledger holds no readable registration
    timestamp policy can act on. Raised before anything is written: never a partial manifest."""


def _age_days(rec: dict[str, Any], moment: datetime) -> float | None:
    """The intent's age in days, or None when it carries no readable registered_at (a pending record
    from before this timestamp existed, or a malformed one). None is never treated as 0: the caller
    excludes it, the same as an intent proven stale, because Policy C requires proof of freshness
    before an intent may ride into an owner-approved batch, not the absence of proof of staleness."""
    raw = rec.get("registered_at")
    if not isinstance(raw, str):
        return None
    try:
        registered = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if registered.tzinfo is None:
        registered = registered.replace(tzinfo=timezone.utc)
    return (moment - registered).total_seconds() / 86400.0


def _open_queue() -> list[dict[str, Any]]:
    """Pending intents that are registered, not yet approved, and not yet consumed: the same "still
    genuinely open" set crew gates reports (cmd_gates), which is what a manifest exists to close."""
    pending = _read_jsonl(_path(_PENDING))
    approved_ids = {r["call_id"] for r in _read_jsonl(_path(_APPROVALS))}
    consumed_ids = {r["call_id"] for r in _read_jsonl(_path(_CONSUMED))}
    return [p for p in pending if p.get("call_id") not in approved_ids and p.get("call_id") not in consumed_ids]


def build_queue_manifest(*, batch_id: str | None = None, retention_days: float = DEFAULT_QUEUE_RETENTION_DAYS,
                         cap: int = DEFAULT_QUEUE_CAP, now: datetime | None = None) -> dict[str, Any]:
    """Policy C's missing middle step: turns the accumulated open queue into the manifest
    `approve_batch` consumes. Read only; makes no network call and calls neither `gated_call` nor
    `judge.call`, directly or indirectly.

    Returns {"manifest": {...}, "summary": {...}}. `manifest` carries only "batch_id" and "calls"
    (each {"call_id", "fingerprint"}, the two fields `approve_batch` actually checks): no task text,
    no role text, no key material, matching the shape the calibration manifest builders already
    write (cmd_calibrate_manifest, cmd_calibrate_manifest_roles). `summary` is the human-readable
    trace a hash alone cannot carry: the call count grouped by purpose and by data category, the age
    range in days over the eligible calls, and every excluded intent with its reason and age.

    Raises QueueManifestRefused, writing nothing, when the eligible count exceeds `cap` (A5): a
    caller must never receive a manifest that silently dropped calls past a size limit, since the
    owner reading only the sha256 would have no way to tell a capped batch from a complete one.

    Also refuses a retention window that is not a real, finite, non negative number of days. A NaN
    window makes `age > retention_days` false for every intent, so the retention rule silently stops
    applying and every stale intent enters the batch; an infinite window means the same thing said
    out loud. Both are the retention guard disabled by a value rather than by a decision, which is
    the shape this guard exists to prevent, so they are refused here rather than at the parser: the
    comparison lives in this function and so must the rule that keeps it meaningful."""
    # A NaN fails every ordered comparison, so `not (x >= 0)` is what catches it; a plain `x < 0`
    # would let it through. Infinity is ordered normally and needs its own test.
    if not (retention_days >= 0.0) or retention_days == float("inf"):
        raise QueueManifestRefused(
            f"retention window must be a finite, non negative number of days, got {retention_days!r}; "
            f"a non finite window disables the retention rule and admits every stale intent")
    moment = now or datetime.now(timezone.utc)
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for rec in _open_queue():
        age = _age_days(rec, moment)
        if age is None:
            excluded.append({"call_id": rec.get("call_id"), "reason": "no_registration_timestamp",
                             "age_days": None})
            continue
        if age > retention_days:
            excluded.append({"call_id": rec.get("call_id"), "reason": "retention_expired",
                             "age_days": round(age, 6)})
            continue
        eligible.append({**rec, "_age_days": age})
    if len(eligible) > cap:
        raise QueueManifestRefused(f"eligible queue is {len(eligible)} call(s), exceeding the cap of {cap}; "
                                   "narrow the retention window or raise --cap explicitly, never truncated "
                                   "silently")
    eligible.sort(key=lambda r: str(r.get("call_id")))
    calls = [{"call_id": r["call_id"], "fingerprint": r["fingerprint"]} for r in eligible]
    if batch_id is None:
        digest_src = json.dumps([[c["call_id"], c["fingerprint"]] for c in calls], sort_keys=True)
        batch_id = f"queue-manifest-{hashlib.sha256(digest_src.encode('utf-8')).hexdigest()[:16]}"
    manifest = {"batch_id": batch_id, "calls": calls}
    by_purpose: dict[str, int] = {}
    by_data_category: dict[str, int] = {}
    for r in eligible:
        by_purpose[str(r.get("purpose"))] = by_purpose.get(str(r.get("purpose")), 0) + 1
        by_data_category[str(r.get("data_category"))] = by_data_category.get(str(r.get("data_category")), 0) + 1
    ages = [r["_age_days"] for r in eligible]
    summary = {
        "call_count": len(eligible),
        "by_purpose": by_purpose,
        "by_data_category": by_data_category,
        "age_range_days": {"min": round(min(ages), 6) if ages else None,
                           "max": round(max(ages), 6) if ages else None},
        "excluded": excluded,
        "retention_days": retention_days,
        "cap": cap,
        "built_at": moment.isoformat(),
    }
    return {"manifest": manifest, "summary": summary}


def _consume_if_approved(fp: str) -> str | None:
    with _locked():
        pending = _read_jsonl(_path(_PENDING))
        approved_ids = {r["call_id"] for r in _read_jsonl(_path(_APPROVALS))}
        consumed_ids = {r["call_id"] for r in _read_jsonl(_path(_CONSUMED))}
        candidates = [p for p in pending if p.get("fingerprint") == fp and p.get("call_id") in approved_ids
                      and p.get("call_id") not in consumed_ids]
        if not candidates:
            return None
        chosen = min(candidates, key=lambda p: p["attempt"])
        call_id = str(chosen["call_id"])
        _append_jsonl(_path(_CONSUMED), {"call_id": call_id, "fingerprint": fp, "consumed_at": _now()})
        return call_id


def gated_call(body: dict[str, Any], *, purpose: str, requirement_ids: Iterable[str], data_category: str,
               includes_worker_output: bool, decline_behavior: str,
               transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    active_transport = transport if transport is not None else (lambda b: judge.call(b, retries=0))
    fp = fingerprint(body)
    consumed_call_id = _consume_if_approved(fp)
    if consumed_call_id is None:
        pkt = packet(body, purpose=purpose, requirement_ids=requirement_ids, data_category=data_category,
                     includes_worker_output=includes_worker_output, decline_behavior=decline_behavior)
        register(pkt)
        return {"state": "owner_gate", "packet": pkt, "response": None, "reason": None}
    try:
        response = active_transport(body)
    except judge.JudgeUnavailable as exc:
        return {"state": "failed", "packet": None, "response": None, "reason": str(exc)}
    return {"state": "answered", "packet": None, "response": response, "reason": None}
