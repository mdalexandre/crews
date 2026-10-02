"""Phase 5: the TypeSafe judge, offline. Every network call goes to a stub on 127.0.0.1."""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from crews import catalog, cli, egress, judge

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
RESPONSES = json.loads((FIX / "judge" / "responses1.json").read_text(encoding="utf-8"))["responses"]
CAT = catalog.load()
# JEV-17: the installed catalog's typesafe.need_band now carries a real provenance.model, and
# crews.need_gate.gate_need refuses to admit a role band that records no model when the caller
# passes one (mirroring the criteria hash absence branch JEV-25 already closed). Read the real
# installed model from the catalog rather than typing a literal, matching how
# tests/test_answer_source.py and tests/test_server.py source it.
_INSTALLED_NEED_MODEL = ((CAT.get("typesafe") or {}).get("need_band") or {}).get("provenance", {}).get("model")


class Stub:
    """Scripted responses: each item is (status, headers, body dict, delay seconds)."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], dict[str, Any], float]] = []
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({"auth": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(length))})
                status, headers, body, delay = stub.script.pop(0) if stub.script else (500, {}, {}, 0.0)
                time.sleep(delay)
                data = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stub]:
    s = Stub()
    monkeypatch.setenv("CREW_TYPESAFE_URL", s.url)
    monkeypatch.setenv("CREW_TYPESAFE_TIMEOUT", "0.5")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY_FILE", raising=False)
    yield s
    s.server.shutdown()


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise OSError(f"test guard: outbound connection to {host} refused")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def egress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TSI-GATE-01: every live judge call is now gated through crews/egress.py's owner approval ledger.
    Keep that ledger, the calibration dir (need_gate bands), and the audit dir under this test's tmp_path,
    the same pattern tests/test_typesafe_wiring.py's egress_env fixture uses."""
    monkeypatch.setenv("CREWS_EGRESS_DIR", str(tmp_path / "egress"))
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    monkeypatch.setenv("CREWS_AUDIT_DIR", str(tmp_path / "audit"))


def _install_role_band(cat_dir: Path, act_threshold: float, confirm_threshold: float) -> None:
    """JEV-12/JEV-25: a role level band, provenanced against the catalog's current need criteria hash
    (the same pattern tests/test_answer_source.py's _install_role_band uses), so a live role answer is
    actually admitted rather than falling back to the declared one with no measured band to gate against."""
    cat_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "act": {"status": "MEASURED", "threshold": act_threshold, "n": 40, "k": 38,
               "precision": 0.95, "wilson_lower": 0.85},
        "confirm": {"status": "MEASURED", "threshold": confirm_threshold, "n": 40, "k": 30,
                   "precision": 0.75, "wilson_lower": 0.62},
        "provenance": {"criteria_hash": cli._need_criteria_hash(CAT), "catalog_version": CAT.get("version"),
                       "model": _INSTALLED_NEED_MODEL,
                       "n_observations": 40, "observations_path": "/tmp/does-not-matter/observations.jsonl",
                       "measured_at": "2026-09-22T19:00:00+00:00"},
    }
    (cat_dir / "bands_roles.json").write_text(json.dumps(payload), encoding="utf-8")


def approve_gates(result: dict[str, Any]) -> None:
    """Approve every pending TypeSafe call a live judge_mode CLI invocation registered (TSI-GATE-01):
    the same approve-then-call pattern tests/test_typesafe_wiring.py's approve_gate() drives."""
    for pkt in result.get("owner_gates") or []:
        egress.approve(pkt["call_id"], pkt["fingerprint"])


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def ok(body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any], float]:
    return (200, {"Content-Type": "application/json"}, body, 0.0)


def role_response(n: int, specific: list[float]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for i in range(n):
        answers[f"need.{i}"] = {"choice": "difficult_review", "confidence": 0.9,
                                "probabilities": {"difficult_review": 0.9}}
        answers[f"difficulty.{i}"] = {"score": 1.2}
        answers[f"divisible.{i}"] = {"noul": 0.2}
        answers[f"specific.{i}"] = {"noul": specific[i]}
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 900}, "answers": answers}




def test_judge_request_shapes(stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    roles = json.loads((FIX / "roles" / "security_review.json").read_text(encoding="utf-8"))
    rf = tmp_path / "roles.json"
    rf.write_text(json.dumps(roles), encoding="utf-8")
    # TSI-GATE-01: the first live call only registers the two pending TypeSafe calls (request 1, request 2)
    # and reaches the stub not at all; approve both before the answered call.
    gate_code, gate_out, gate_err = run_cli(capsys, "plan", "--roles", str(rf), "--judge", "live", "--allow", "5",
                                            "--run-dir", str(tmp_path / "gate"))
    assert gate_code == 0, gate_err
    approve_gates(json.loads(gate_out))
    assert stub.requests == []
    stub.script = [ok(RESPONSES[3]), ok(role_response(4, [0.9, 0.9, 0.9, 0.9]))]
    code, out, err = run_cli(capsys, "plan", "--roles", str(rf), "--judge", "live", "--allow", "5",
                             "--run-dir", str(tmp_path / "r"))
    assert code == 0, err
    first, second = (r["body"] for r in stub.requests)
    # round three protocol.md section 4 rewrote crews/catalog.json's questions.need.criteria text (the
    # expanded contrast rules the round three activation measured its band against, JEV-25), while the
    # external reference prototype at REFERENCE still carries the older, terser criteria descriptions.
    # The category names themselves did not change, so the live request must still carry exactly the
    # catalog's own criteria (proving request1 sends cat["questions"] verbatim, crews/judge.py request1),
    # and every other question must still match the reference prototype's fixed shape unchanged.
    expected_questions = copy.deepcopy(CAT["questions"])
    assert first["questions"] == expected_questions
    assert set(first["state"]) == {"task"} and first["model"] == "jev-latest"
    expected = {f"{q}.{i}" for i in range(4) for q in ("need", "difficulty", "divisible", "specific")}
    assert set(second["questions"]) == expected
    assert set(second["state"]) == {"task", "roles"} and second["model"] == "jev-latest"


def test_judge_parse_recorded_responses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert len(RESPONSES) >= 5
    for i, resp in enumerate(RESPONSES):
        f = tmp_path / f"r{i}.json"
        f.write_text(json.dumps(resp), encoding="utf-8")
        code, out, err = run_cli(capsys, "parse", "--response", str(f))
        assert code == 0, err
        assert set(json.loads(out)) == {"need", "divisible", "needs_verifier", "difficulty"}


@pytest.mark.parametrize("mutate,field", [
    (lambda a: a["need"].__setitem__("choice", "astrology"), "need.choice"),
    (lambda a: a.pop("difficulty"), "difficulty"),
    (lambda a: a["divisible"].__setitem__("noul", 1.4), "divisible.noul"),
    (lambda a: a["difficulty"].__setitem__("score", 3.5), "difficulty.score"),
])
def test_judge_parse_refuses_bad_fields(mutate: Any, field: str, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    resp = copy.deepcopy(RESPONSES[0])
    mutate(resp["answers"])
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(resp), encoding="utf-8")
    code, out, err = run_cli(capsys, "parse", "--response", str(f))
    assert code == 2 and field in err


@pytest.mark.parametrize("script", [
    [(500, {}, {}, 0.0)],
    [(200, {}, {}, 2.0)],
    [(429, {"retry-after": "0"}, {}, 0.0)] * 3,
], ids=["http500", "timeout", "429-past-cap"])
def test_judge_outage_falls_back(script: list[Any], stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    task = "refactor the billing adapter"
    gate_code, gate_out, gate_err = run_cli(capsys, "budget", "--task", task, "--judge", "live")
    assert gate_code == 0, gate_err
    approve_gates(json.loads(gate_out))
    assert stub.requests == []
    stub.script = list(script)
    code, out, err = run_cli(capsys, "budget", "--task", task, "--judge", "live")
    assert code == 0, err
    result = json.loads(out)
    assert result["budget"] == 2 and result["confidence"] == 0.0 and result["check_required"] is True
    assert result["judge"]["status"] == "outage"
    assert any("judge unavailable" in n for n in result["notes"])
    # CR-1/INV-03: the gated path calls transport with retries=0, so exactly one request reaches the stub
    # regardless of how many outage responses the script scaffolds.
    assert len(stub.requests) == 1


class Recorder(dict[str, str]):
    """An environment that remembers which keys were asked for."""

    def __init__(self, base: dict[str, str]) -> None:
        super().__init__(base)
        self.asked: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self.asked.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self.asked.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self.asked.add(str(key))
        return super().__contains__(key)


@pytest.mark.parametrize("via", ["env", "file"])
def test_judge_secret_never_leaks(via: str, stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = f"SENTINEL-{via.upper()}-ae51c0d9"
    if via == "env":
        monkeypatch.setenv("TYPESAFE_API_KEY", sentinel)
    else:
        key_file = tmp_path / "key"
        key_file.write_text(sentinel + "\n", encoding="utf-8")
        monkeypatch.setenv("TYPESAFE_API_KEY_FILE", str(key_file))
    run_dir = tmp_path / "run"
    task = "refactor the billing adapter"
    seen = []
    # TSI-GATE-01: each live call needs its own approval (fingerprint attempts are single use), so gate then
    # approve then call once per scripted outcome, rather than one direct call per outcome as before.
    outcomes: tuple[tuple[int, dict[str, str], dict[str, Any], float], ...] = (
        ok(RESPONSES[2]), (500, {}, {"error": "boom"}, 0.0))
    for outcome in outcomes:
        gate_code, gate_out, gate_err = run_cli(capsys, "budget", "--task", task, "--judge", "live",
                                                "--run-dir", str(run_dir))
        assert gate_code == 0, gate_err
        seen += [gate_out, gate_err]
        approve_gates(json.loads(gate_out))
        stub.script = [outcome]
        code, out, err = run_cli(capsys, "budget", "--task", task, "--judge", "live", "--run-dir", str(run_dir))
        assert code == 0
        seen += [out, err]
    assert stub.requests[0]["auth"] == f"Bearer {sentinel}"
    files = [p.read_text(encoding="utf-8") for p in run_dir.rglob("*") if p.is_file()]
    assert files and not any(sentinel in text for text in seen + files)


def test_judge_off_never_reads_the_key(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(dict(os.environ))
    monkeypatch.setattr(os, "environ", rec)
    f = tmp_path / "a.json"
    f.write_text(json.dumps(RESPONSES[0]["answers"]), encoding="utf-8")
    code, out, err = run_cli(capsys, "budget", "--answers", str(f), "--judge", "off")
    assert code == 0, err
    roles = FIX / "roles" / "rename_button.json"
    code, out, err = run_cli(capsys, "plan", "--roles", str(roles), "--answers", str(f),
                             "--run-dir", str(tmp_path / "r"))
    assert code == 0, err
    assert not rec.asked & {"TYPESAFE_API_KEY", "TYPESAFE_API_KEY_FILE"}


def test_judge_no_network_and_retry_after(stub: Stub, capsys: pytest.CaptureFixture[str],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 443), timeout=0.2)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    waits: list[float] = []
    monkeypatch.setattr(judge, "_sleep", lambda s: waits.append(s))
    task = "caption 40 listings"
    gate_code, gate_out, gate_err = run_cli(capsys, "budget", "--task", task, "--judge", "live")
    assert gate_code == 0, gate_err
    approve_gates(json.loads(gate_out))
    assert stub.requests == []
    stub.script = [(429, {"retry-after": "0.25"}, {}, 0.0)] * 5
    code, out, err = run_cli(capsys, "budget", "--task", task, "--judge", "live")
    assert code == 0
    # CR-1/INV-03: egress.gated_call's transport is judge.call(body, retries=0), so a single approval
    # authorizes exactly one network attempt and never an automatic retry (waits stays empty).
    assert len(stub.requests) == 1
    assert waits == []
    assert json.loads(out)["judge"]["reason"] == "HTTP 429 after retries"


def test_judge_parse2_confidence_present() -> None:
    resp = role_response(2, [0.9, 0.3])
    out = judge.parse2_confidence(CAT, resp, 2)
    assert len(out) == 2
    for row in out:
        assert row["need"]["choice"] == "difficult_review"
        assert row["need"]["confidence"] == 0.9
        assert row["need"]["probabilities"] == {"difficult_review": 0.9}
        assert set(row) == {"need", "difficulty", "divisible", "specific"}


def test_judge_parse2_confidence_absent_is_none() -> None:
    resp = role_response(1, [0.9])
    del resp["answers"]["need.0"]["confidence"]
    del resp["answers"]["need.0"]["probabilities"]
    out = judge.parse2_confidence(CAT, resp, 1)
    assert out[0]["need"]["confidence"] is None
    assert out[0]["need"]["probabilities"] is None
    assert out[0]["need"]["choice"] == "difficult_review"


def test_judge_parse2_confidence_malformed_raises() -> None:
    resp = role_response(1, [0.9])
    resp["answers"]["need.0"]["confidence"] = "high"
    with pytest.raises(judge.JudgeParseError, match="need.0.confidence"):
        judge.parse2_confidence(CAT, resp, 1)


def test_judge_parse2_shape_unchanged_by_confidence_exposure() -> None:
    resp = role_response(3, [0.9, 0.9, 0.9])
    out = judge.parse2(CAT, resp, 3)
    assert len(out) == 3
    for row in out:
        assert set(row) == {"need", "difficulty", "divisible", "specific"}
        assert row["need"] == "difficult_review"




@pytest.mark.parametrize("specific,code", [(0.3, 2), (0.5, 0)])
def test_judge_specificity_gate(specific: float, code: int, stub: Stub, tmp_path: Path,
                                capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    roles = json.loads((FIX / "roles" / "refactor_billing.json").read_text(encoding="utf-8"))
    rf = tmp_path / "roles.json"
    rf.write_text(json.dumps(roles), encoding="utf-8")
    # JEV-12: a live role answer is now gated by need_gate against a role level band, the same way
    # the task need answer is; with no measured role band the decision is uncalibrated and the
    # declared answer (specific=0.9, never below the floor) stands, so the specificity gate this test
    # exists to prove would never fire. Install a role band, provenanced against the current need
    # criteria hash, whose act threshold sits below the 0.9 confidence role_response() reports, so the
    # live judged specific score (0.3 or 0.5) is the one planner.build actually gates on.
    _install_role_band(tmp_path / "calibration", act_threshold=0.85, confirm_threshold=0.55)
    gate_code, gate_out, gate_err = run_cli(capsys, "plan", "--roles", str(rf), "--judge", "live",
                                            "--run-dir", str(tmp_path / "gate"))
    assert gate_code == 0, gate_err
    approve_gates(json.loads(gate_out))
    assert stub.requests == []
    stub.script = [ok(RESPONSES[2]), ok(role_response(2, [0.9, specific]))]
    got, out, err = run_cli(capsys, "plan", "--roles", str(rf), "--judge", "live", "--run-dir", str(tmp_path / "r"))
    assert got == code, err
    if code == 2:
        assert "billing-parity-checker" in err and "specificity" in err


def test_judge_403_reason_from_json_body(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner directive 2026-09-23: a 403 with a JSON error body now names the endpoint's own reason."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    stub.script = [(403, {}, {"error": "api key revoked"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403: api key revoked"


def test_judge_403_prefers_message_then_detail_then_code(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    stub.script = [(403, {}, {"message": "trial plan lapsed"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403: trial plan lapsed"
    stub.script = [(403, {}, {"detail": "quota tier exceeded"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403: quota tier exceeded"
    stub.script = [(403, {}, {"code": "key_expired"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403: key_expired"


def test_judge_403_scrubs_echoed_key(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one case where a diagnostic improvement could become a credential leak: an endpoint that
    echoes the Authorization header, or the raw key, back inside the error body."""
    key = "synthetic-live-key-9f2c7a"  # gitleaks:allow (synthetic fixture, not a credential)
    monkeypatch.setenv("TYPESAFE_API_KEY", key)
    stub.script = [(403, {}, {"error": f"invalid credential: Authorization: Bearer {key}"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    message = str(excinfo.value)
    assert "[redacted]" in message
    assert key not in message


def test_judge_403_credential_split_across_newlines_and_tabs_is_scrubbed(stub: Stub,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: `_safe` normalizes with `_clamp` before it redacts with `_scrub` (the order the `_safe`
    docstring names as the fix for CWE-180). A body that echoes the key with newlines and tabs where
    the key itself carries plain spaces collapses to one contiguous run before the scrub runs, so the
    literal credential never survives into `_reason_from_body`'s return value."""
    key = "synthetic key 9f2c7a"
    monkeypatch.setenv("TYPESAFE_API_KEY", key)
    leaked = "credential: synthetic\nkey\t9f2c7a leaked"
    stub.script = [(403, {}, {"error": leaked}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    message = str(excinfo.value)
    assert key not in message
    assert "[redacted]" in message


class _FakeBody:
    """Stands in for the parts of urllib.error.HTTPError that _reason_from_body reads: just `.read(n)`.
    Exercises the diagnostic path directly and deterministically, at the byte level a live HTTP stub
    cannot cheaply reproduce (an already-closed stream, invalid UTF-8, a read that hits the cap)."""

    def __init__(self, data: bytes | None = None, raises: Exception | None = None) -> None:
        self._data = data
        self._raises = raises
        self.read_calls = 0

    def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._data is not None
        return self._data


@pytest.mark.parametrize("make_body,case", [
    (lambda: _FakeBody(raises=ValueError("I/O operation on closed file")), "already-consumed"),
    (lambda: _FakeBody(data=b""), "empty"),
    (lambda: _FakeBody(data=b"\xff\xfe not valid utf-8 \x80\x81"), "invalid-utf8"),
    (lambda: _FakeBody(data=b"<html><body>502 Bad Gateway</body></html>"), "non-json-html"),
    (lambda: _FakeBody(data=b'{"error": "x' + b"y" * judge.BODY_READ_CAP_BYTES), "oversized"),
], ids=["already-consumed", "empty", "invalid-utf8", "non-json-html", "oversized"])
def test_judge_403_degenerate_bodies_never_raise_and_return_empty(make_body: Any, case: str) -> None:
    """A3: every degenerate body yields "" from _reason_from_body, which call() turns into the plain
    HTTP {code} message; _reason_from_body itself never raises for any of these five cases."""
    fake = make_body()
    assert judge._reason_from_body(fake, "some-key") == "", case  # type: ignore[arg-type]


def test_judge_403_oversized_read_is_capped_at_the_stated_bytes() -> None:
    """A4: the read itself is bounded to BODY_READ_CAP_BYTES, so an oversized body is truncated at read
    time rather than ever carried whole into this process."""
    fake = _FakeBody(data=b'{"error": "x' + b"y" * judge.BODY_READ_CAP_BYTES)
    judge._reason_from_body(fake, "some-key")  # type: ignore[arg-type]
    assert fake.read_calls == 1


def test_judge_403_general_non_json_text_falls_back_to_snippet() -> None:
    """The fallback the instructions name for a non JSON, non HTML body: a truncated single line
    snippet of the text, scrubbed of the key."""
    fake = _FakeBody(data=b"rate limit exceeded for this plan, upgrade at typesafe.ai/billing")
    reason = judge._reason_from_body(fake, "some-key")  # type: ignore[arg-type]
    assert reason == "rate limit exceeded for this plan, upgrade at typesafe.ai/billing"


def test_judge_403_terminal_error_carries_the_request_id_header(stub: Stub,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """A2: the terminal HTTPError branch reads the `x-typesafe-request-id` response header and appends
    it to the JudgeUnavailable message, so an operator can trace a specific rejection with TypeSafe
    support."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    stub.script = [(403, {"x-typesafe-request-id": "req-abc123"}, {"error": "api key revoked"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403: api key revoked [request-id req-abc123]"


def test_judge_403_absent_request_id_leaves_message_byte_identical(stub: Stub,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A3: with no `x-typesafe-request-id` header, the message is exactly the pre-existing
    `HTTP {code}: {reason}` format, unchanged by the new header capture."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    stub.script = [(403, {}, {"error": "api key revoked"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403: api key revoked"


def test_judge_403_via_call_with_non_dict_json_body_falls_back_to_plain(stub: Stub,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee through the real call() path: a 403 whose body parses as JSON but is not an
    object (here a bare list) carries no `error`/`message`/`detail`/`code` field, so the plain
    message is used, and call() never raises anything but JudgeUnavailable."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    stub.script = [(403, {}, [1, 2, 3], 0.0)]  # type: ignore[list-item]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"})
    assert str(excinfo.value) == "HTTP 403"


def test_judge_403_never_retries(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    waits: list[float] = []
    monkeypatch.setattr(judge, "_sleep", lambda s: waits.append(s))
    stub.script = [(403, {}, {"error": "revoked"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable):
        judge.call({"model": "jev-latest"}, retries=2)
    assert len(stub.requests) == 1
    assert waits == []


def test_judge_429_retry_path_unaffected_by_body_reading(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    """A5: the 429/529 retry decision reads only exc.headers.get('retry-after'); proves the added body
    read on the terminal branch does not consume anything the retry branch needs, by retrying twice on
    empty 429 bodies and then reading a real reason off the exhausting third response."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    waits: list[float] = []
    monkeypatch.setattr(judge, "_sleep", lambda s: waits.append(s))
    stub.script = [(429, {"retry-after": "0"}, {}, 0.0), (429, {"retry-after": "0"}, {}, 0.0),
                   (429, {"retry-after": "0"}, {"error": "quota tier exhausted"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"}, retries=2)
    assert len(stub.requests) == 3
    assert waits == [0.0, 0.0]
    assert str(excinfo.value) == "HTTP 429 after retries: quota tier exhausted"


def test_judge_retries_zero_is_one_attempt(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    stub.script = [(429, {"retry-after": "0"}, {"error": "should never be retried"}, 0.0)]
    with pytest.raises(judge.JudgeUnavailable) as excinfo:
        judge.call({"model": "jev-latest"}, retries=0)
    assert len(stub.requests) == 1
    assert str(excinfo.value) == "HTTP 429 after retries: should never be retried"


def test_judge_success_path_scrub_call_unchanged(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    """A7: the success path still runs through the existing `_scrub` call at the single `json.loads`
    return site (crews/judge.py, `return parsed` in `call`'s try block); no line there changed."""
    key = "synthetic-test-key"
    monkeypatch.setenv("TYPESAFE_API_KEY", key)
    stub.script = [(200, {}, {"model": "jev-1.13.0", "usage": {"input_tokens": 5},
                              "answers": {"echo": key}}, 0.0)]
    result = judge.call({"model": "jev-latest"})
    assert result["answers"]["echo"] == "[redacted]"
    assert key not in json.dumps(result)


def test_judge_cli_subprocess_off_is_offline(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_text(json.dumps(RESPONSES[1]["answers"]), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TYPESAFE")}
    r = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "crew.py"), "budget", "--answers", str(f)],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout)["budget"] == 5
