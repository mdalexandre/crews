"""C-HB: hand back probe judging (typesafe-integration-v1.0.md section 2.5, C-HB; requirements TSI-HB-01
to TSI-HB-03, TSI-GATE-05).

A hand back is the text a role returns, captured by `crew record`. This module runs a mechanical check
before any semantic request is built (a malformed hand back never reaches the judge), builds the Noul
request that asks whether a claimed-PASS criterion actually cites an observed probe rather than a
restatement, and applies the returned Nouls to turn a restatement-backed PASS into UNVERIFIED. It never
decides whether a hand back is genuine on its own: that semantic call belongs to Jev. This module never
calls the network; request bodies are handed to `crews/egress.py` by the caller.

Interface
    check_mechanical(text, criteria_ids) -> MechanicalResult      MALFORMED before any request is built
    request_check(text, criteria, pass_ids) -> body                one probe.<id> Noul per PASS id
    parse_probe_response(response, pass_ids) -> {id: noul}
    artifact_mechanical(text, acceptance_ids) -> MechanicalResult
    request_artifact(text, acceptance, ids) -> body                 one verify.<id> Noul per acceptance id
    parse_verify_response(response, ids) -> {id: noul}
    apply(parsed, nouls, threshold, calibrated=True) -> dict        the judged verdict rule

Parsing of the check role's fixed VERDICT/PASS/FAIL/UNVERIFIED/DECISION block reuses
`crews/calibration/verdict.py` without modification (parent authority: SRD section 2.5 C-HB).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from crews import verdict as _verdict

MODEL = "jev-latest"

# TSI-GATE-05: a request built from a hand back or a generated brief always carries worker output, so the
# egress packet built from either request builder in this module must be marked accordingly.
DATA_CATEGORY = "worker_handback_text"
INCLUDES_WORKER_OUTPUT = True

_STATUS_RE = re.compile(r"^STATUS:\s*(COMPLETE|PARTIAL|BLOCKED)\s*$")
_VERIFY_RE = re.compile(r"^VERIFY:\s*(.*)$")
_UNVERIFIED_ID_RE = re.compile(r"^([^\s:]+):\s*(.*)$")


class HandbackParseError(ValueError):
    """A judge response does not match a probe.<id> or verify.<id> question. Names the field."""


@dataclass(frozen=True)
class MechanicalResult:
    """`status` is "OK" or "MALFORMED". Exactly one of `verdict` (check mode) or `parsed` (artifact mode)
    is set when status is "OK"; `reason` is set when status is "MALFORMED"."""
    status: str
    reason: str | None = None
    verdict: _verdict.Verdict | None = None
    parsed: "ParsedArtifact | None" = None


@dataclass(frozen=True)
class ParsedArtifact:
    top_status: str
    verify_ids: frozenset[str]
    unverified_ids: frozenset[str]
    verify_text: str


def _single_unverified_id(raw: str) -> str | None:
    """The fixed contract's UNVERIFIED line is "NONE|<ID: reason>": at most one criterion id. Returns None
    for "NONE" and for text that carries no leading "<id>:" token (the caller treats that as malformed)."""
    if raw == "NONE":
        return None
    m = _UNVERIFIED_ID_RE.match(raw)
    return m.group(1) if m else None


def _coverage_defect(criteria_ids: Collection[str], passed_ids: Collection[str], failed_ids: Collection[str],
                      unverified_id: str | None) -> str | None:
    counts: Counter[str] = Counter(passed_ids) + Counter(cid for cid in failed_ids)
    if unverified_id is not None:
        counts[unverified_id] += 1
    dupes = sorted(cid for cid, n in counts.items() if n > 1)
    if dupes:
        return f"criterion {dupes[0]} listed more than once across PASS, FAIL, and UNVERIFIED"
    known = set(criteria_ids)
    covered = set(counts)
    missing = sorted(known - covered)
    if missing:
        return f"criterion {missing[0]} is not accounted for in PASS, FAIL, or UNVERIFIED"
    unknown = sorted(covered - known)
    if unknown:
        return f"criterion {unknown[0]} is not one of the acceptance criteria"
    return None


def check_mechanical(text: str, criteria_ids: Collection[str]) -> MechanicalResult:
    """VERDICT line present, every criterion id in exactly one of PASS, FAIL, UNVERIFIED, every FAIL line
    carries EVIDENCE (enforced by `verdict.parse_verdict_block`, reused unmodified). Any defect gives
    MALFORMED with no request built (TSI-HB-01)."""
    try:
        v = _verdict.parse_verdict_block(text.splitlines())
    except _verdict.VerdictParseError as exc:
        return MechanicalResult(status="MALFORMED", reason=str(exc))
    if v.unverified_raw != "NONE" and _single_unverified_id(v.unverified_raw) is None:
        return MechanicalResult(
            status="MALFORMED",
            reason=f"UNVERIFIED: could not extract a criterion id from {v.unverified_raw!r}")
    unverified_id = _single_unverified_id(v.unverified_raw)
    defect = _coverage_defect(criteria_ids, v.passed_ids, v.failed, unverified_id)
    if defect is not None:
        return MechanicalResult(status="MALFORMED", reason=defect)
    return MechanicalResult(status="OK", verdict=v)


def request_check(text: str, criteria: dict[str, str], pass_ids: Collection[str]) -> dict[str, Any]:
    """One `probe.<id>` Noul per PASS id. State carries the criteria and the hand back text verbatim
    (TSI-GATE-05: this state is worker output and must be marked `includes_worker_output` by the caller
    that packets it through `egress.gated_call`)."""
    questions: dict[str, Any] = {}
    for cid in sorted(pass_ids):
        questions[f"probe.{cid}"] = {
            "type": "noul",
            "instructions": (
                f"This hand back marks criterion {cid} ({criteria.get(cid, '')!r}) as PASS. Does the hand "
                "back cite an observed probe result, an actual command run, a file read, or a test "
                "executed, as evidence the criterion holds, rather than only restating the criterion, "
                "paraphrasing the producer's own conclusion, or claiming a test result with no shown "
                "output?"),
        }
    return {"model": MODEL, "state": {"criteria": criteria, "handback": text}, "questions": questions}


def artifact_mechanical(text: str, acceptance_ids: Collection[str]) -> MechanicalResult:
    """STATUS and VERIFY present. Any defect gives MALFORMED with no request built."""
    lines = [line for line in text.splitlines() if line.strip() != ""]
    status_line = next((line for line in lines if line.strip().startswith("STATUS:")), None)
    if status_line is None:
        return MechanicalResult(status="MALFORMED", reason="no STATUS line")
    m = _STATUS_RE.match(status_line.strip())
    if not m:
        return MechanicalResult(status="MALFORMED", reason=f"STATUS line malformed: {status_line!r}")
    verify_line = next((line for line in lines if line.strip().startswith("VERIFY:")), None)
    if verify_line is None:
        return MechanicalResult(status="MALFORMED", reason="no VERIFY line")
    vm = _VERIFY_RE.match(verify_line.strip())
    verify_text = (vm.group(1).strip() if vm else "")
    if not verify_text:
        return MechanicalResult(status="MALFORMED", reason="VERIFY line is empty")
    known = set(acceptance_ids)
    verify_ids = frozenset(cid for cid in known if re.search(rf"(?<![\w.]){re.escape(cid)}(?![\w.])", verify_text))
    unverified_ids = frozenset(known - verify_ids)
    parsed = ParsedArtifact(top_status=m.group(1), verify_ids=verify_ids, unverified_ids=unverified_ids,
                             verify_text=verify_text)
    return MechanicalResult(status="OK", parsed=parsed)


def request_artifact(text: str, acceptance: dict[str, str], ids: Collection[str]) -> dict[str, Any]:
    """One `verify.<id>` Noul per acceptance id."""
    questions: dict[str, Any] = {}
    for cid in sorted(ids):
        questions[f"verify.{cid}"] = {
            "type": "noul",
            "instructions": (
                f"The VERIFY line of this hand back claims acceptance criterion {cid} "
                f"({acceptance.get(cid, '')!r}) is met. Does the hand back cite an observed check command "
                "and its result as evidence, rather than only restating the criterion or the requester's "
                "own claim?"),
        }
    return {"model": MODEL, "state": {"acceptance": acceptance, "handback": text}, "questions": questions}


def _answers(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        raise HandbackParseError("answers: missing")
    out: dict[str, Any] = response["answers"]
    return out


def _noul(ans: dict[str, Any], qid: str) -> float:
    a = ans.get(qid)
    if not isinstance(a, dict):
        raise HandbackParseError(f"{qid}: missing answer")
    v = a.get("noul")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise HandbackParseError(f"{qid}.noul: must be a probability from 0 to 1")
    return float(v)


def parse_probe_response(response: Any, pass_ids: Collection[str]) -> dict[str, float]:
    ans = _answers(response)
    return {cid: _noul(ans, f"probe.{cid}") for cid in pass_ids}


def parse_verify_response(response: Any, ids: Collection[str]) -> dict[str, float]:
    ans = _answers(response)
    return {cid: _noul(ans, f"verify.{cid}") for cid in ids}


def _overall(per_criterion: dict[str, str]) -> str:
    states = set(per_criterion.values())
    if "FAIL" in states:
        return "FAIL"
    if states and states <= {"PASS"}:
        return "PASS"
    return "UNVERIFIED"


def apply(parsed: _verdict.Verdict | ParsedArtifact, nouls: dict[str, float] | None,
          threshold: float, *, calibrated: bool = True) -> dict[str, Any]:
    """A PASS (or verified) id whose Noul is below `threshold` becomes UNVERIFIED with reason "restatement".
    `nouls=None` gives the hand back state UNJUDGED, never PASS (TSI-HB-03), regardless of `calibrated`:
    with no answer there is nothing to record and nothing to apply. Otherwise the judged verdict is PASS
    only when every criterion is PASS after judging, FAIL when any criterion is FAIL, else UNVERIFIED.

    `calibrated` defaults to True so every existing caller keeps today's behavior byte for byte. JEV-17
    (mission SRD): an uncalibrated measurement must never produce an acting decision. Pass
    `calibrated=False` (the catalog's `typesafe.handback.calibrated` flag, threaded through by
    `crews.cli.cmd_judge_handback`) to record the probe Nouls for later calibration without ever letting a
    below-threshold Noul turn a claimed PASS into UNVERIFIED: every claimed PASS id stays "PASS",
    `observed_nouls` carries the raw Nouls that were called and answered, and `observed_decision` carries
    the judged result the threshold would have produced had it been trusted, so an operator reading the
    result can tell it only observed."""
    if isinstance(parsed, _verdict.Verdict):
        claimed_pass = set(parsed.passed_ids)
        claimed_fail = set(parsed.failed)
        unverified_id = _single_unverified_id(parsed.unverified_raw)
        claimed_unverified = {unverified_id} if unverified_id is not None else set()
    else:
        claimed_pass = set(parsed.verify_ids)
        claimed_fail = set()
        claimed_unverified = set(parsed.unverified_ids)

    base: dict[str, str] = {cid: "FAIL" for cid in claimed_fail}
    base.update({cid: "UNVERIFIED" for cid in claimed_unverified})

    if nouls is None:
        per_criterion = dict(base)
        for cid in claimed_pass:
            per_criterion[cid] = "UNJUDGED"
        return {"per_criterion": per_criterion, "verdict": "UNJUDGED", "reasons": {}, "acted": False}

    judged: dict[str, str] = dict(base)
    reasons: dict[str, str] = {}
    for cid in sorted(claimed_pass):
        n = nouls.get(cid)
        if n is None or n < threshold:
            judged[cid] = "UNVERIFIED"
            reasons[cid] = "restatement"
        else:
            judged[cid] = "PASS"

    if not calibrated:
        claim_only = dict(base)
        for cid in claimed_pass:
            claim_only[cid] = "PASS"
        return {"per_criterion": claim_only, "verdict": _overall(claim_only), "reasons": {}, "acted": False,
                "observed_nouls": dict(nouls),
                "observed_decision": {"per_criterion": judged, "verdict": _overall(judged), "reasons": reasons},
                "note": ("uncalibrated probe_threshold (JEV-17): TypeSafe was called and its Nouls "
                         "recorded, but they were never applied to a claimed PASS")}

    return {"per_criterion": judged, "verdict": _overall(judged), "reasons": reasons, "acted": True}
