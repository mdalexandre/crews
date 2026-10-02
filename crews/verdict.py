"""crew calibration verdict: parse the check role's output contract, exactly as built at
crews/planner.py:311-317 ("Return exactly: VERDICT: PASS|FAIL|BLOCKED / PASS: <criterion IDs, comma
separated, or NONE> / FAIL <ID>: <defect> | EVIDENCE: <...> / UNVERIFIED: NONE|<ID: reason> / DECISION:
NONE|<...>").

A calibration --batch dispatch asks a seat to judge several artifacts in one call. Each artifact's block is
still that exact contract; a batch of more than one artifact is expected to separate them with the
calibration harness's own "ARTIFACT: <artifact_id>" header line, which is not part of the fixed check
contract and is never sent to a real crew check dispatch outside this harness.

The contract's third line is "FAIL <ID>: <defect> | EVIDENCE: <...>" and says nothing about the case where
nothing failed, so a seat may either omit the line or write "FAIL: NONE" by analogy with its own "PASS:
<ids or NONE>" and "UNVERIFIED: NONE|<...>" lines. Both renderings were OBSERVED from crew-check-sonnet-high
on 2026-09-20 within one calibration run, so both are conforming and both parse. A "FAIL: NONE" sentinel
asserts there are no failures, so it may not appear beside a real FAIL line in either order; that is a
contradiction, not a rendering choice. A FAIL line that names a defect but drops its EVIDENCE clause stays
an error: discarding it would erase a detection, which is the one direction this parser must never be loose
in.

Interface
    parse_verdict_block(lines) -> Verdict          raises VerdictParseError naming the offending line
    parse_batch_text(text, artifact_ids) -> dict[str, Verdict]
        splits on "ARTIFACT: <id>" headers when more than one id is expected; a single-artifact batch with
        no header is treated as one block for that id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|FAIL|BLOCKED)\s*$")
_PASS_RE = re.compile(r"^PASS:\s*(.*)$")
_FAIL_RE = re.compile(r"^FAIL\s+(\S+):\s*(.*?)\s*\|\s*EVIDENCE:\s*(.*)$")
_FAIL_NONE_RE = re.compile(r"^FAIL:\s*NONE\s*$", re.IGNORECASE)
_UNVERIFIED_RE = re.compile(r"^UNVERIFIED:\s*(.*)$")
_DECISION_RE = re.compile(r"^DECISION:\s*(.*)$")
_ARTIFACT_RE = re.compile(r"^ARTIFACT:\s*(\S+)\s*$")


class VerdictParseError(ValueError):
    """A block does not match the check output contract. The message names the offending line."""


@dataclass(frozen=True)
class Verdict:
    overall: str  # "PASS" | "FAIL" | "BLOCKED"
    passed_ids: frozenset[str]
    failed: dict[str, tuple[str, str]]  # criterion id -> (defect text, evidence)
    unverified_raw: str  # "NONE" or the raw "<ID: reason>" text
    decision: str  # "NONE" or the raw decision text


def _split_ids(raw: str) -> frozenset[str]:
    raw = raw.strip()
    if raw == "" or raw.upper() == "NONE":
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def parse_verdict_block(lines: list[str]) -> Verdict:
    content = [line for line in lines if line.strip() != ""]
    if not content:
        raise VerdictParseError("empty block: no VERDICT line")

    idx = 0
    m = _VERDICT_RE.match(content[idx].strip())
    if not m:
        raise VerdictParseError(f"line {idx + 1}: expected 'VERDICT: PASS|FAIL|BLOCKED', got {content[idx]!r}")
    overall = m.group(1)
    idx += 1

    if idx >= len(content):
        raise VerdictParseError("block ends after VERDICT: missing PASS line")
    m = _PASS_RE.match(content[idx].strip())
    if not m:
        raise VerdictParseError(f"line {idx + 1}: expected 'PASS: <ids or NONE>', got {content[idx]!r}")
    passed_ids = _split_ids(m.group(1))
    idx += 1

    failed: dict[str, tuple[str, str]] = {}
    no_failures_asserted = False
    while idx < len(content):
        line = content[idx].strip()
        if _FAIL_NONE_RE.match(line):
            if failed:
                raise VerdictParseError(
                    f"line {idx + 1}: 'FAIL: NONE' contradicts the {len(failed)} FAIL line(s) above it")
            no_failures_asserted = True
            idx += 1
            continue
        m = _FAIL_RE.match(line)
        if not m:
            break
        if m.group(1).upper() == "NONE":
            # "FAIL NONE: no defect found | EVIDENCE: ..." OBSERVED 2026-09-20. NONE is not a criterion id,
            # so this is the zero-failure case wearing the FAIL line's shape, not a failure on a criterion
            # called NONE. Recording it as one would put a phantom id in `failed`.
            if failed:
                raise VerdictParseError(
                    f"line {idx + 1}: 'FAIL NONE' contradicts the {len(failed)} FAIL line(s) above it")
            no_failures_asserted = True
            idx += 1
            continue
        if no_failures_asserted:
            raise VerdictParseError(
                f"line {idx + 1}: a FAIL line follows 'FAIL: NONE', which asserted there were none")
        cid, defect, evidence = m.group(1), m.group(2), m.group(3)
        failed[cid] = (defect, evidence)
        idx += 1

    if idx >= len(content):
        raise VerdictParseError("block ends before UNVERIFIED line")
    m = _UNVERIFIED_RE.match(content[idx].strip())
    if not m:
        raise VerdictParseError(
            f"line {idx + 1}: expected 'UNVERIFIED: NONE|<ID: reason>', got {content[idx]!r}")
    unverified_raw = m.group(1).strip() or "NONE"
    idx += 1

    if idx >= len(content):
        raise VerdictParseError("block ends before DECISION line")
    m = _DECISION_RE.match(content[idx].strip())
    if not m:
        raise VerdictParseError(f"line {idx + 1}: expected 'DECISION: NONE|<...>', got {content[idx]!r}")
    decision = m.group(1).strip() or "NONE"
    idx += 1

    if idx != len(content):
        raise VerdictParseError(f"line {idx + 1}: unexpected trailing content {content[idx]!r}")

    return Verdict(overall=overall, passed_ids=passed_ids, failed=failed, unverified_raw=unverified_raw,
                    decision=decision)


def parse_batch_text(text: str, artifact_ids: list[str]) -> dict[str, Verdict]:
    lines = text.splitlines()
    headers = [(i, m.group(1)) for i, m in ((i, _ARTIFACT_RE.match(line.strip())) for i, line in enumerate(lines))
               if m]

    if not headers:
        if len(artifact_ids) != 1:
            raise VerdictParseError(
                f"batch of {len(artifact_ids)} artifacts carries no 'ARTIFACT: <id>' headers; "
                f"expected one for each of {artifact_ids}")
        return {artifact_ids[0]: parse_verdict_block(lines)}

    seen: dict[str, Verdict] = {}
    for pos, (start, artifact_id) in enumerate(headers):
        end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        block_lines = lines[start + 1:end]
        if artifact_id in seen:
            raise VerdictParseError(f"artifact id {artifact_id!r} appears more than once in the batch text")
        seen[artifact_id] = parse_verdict_block(block_lines)

    expected = set(artifact_ids)
    got = set(seen)
    if got != expected:
        raise VerdictParseError(
            f"batch text artifact ids {sorted(got)} do not match the requested {sorted(expected)}")
    return seen
