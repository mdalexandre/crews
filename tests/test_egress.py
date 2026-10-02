"""tests/test_egress.py: TSI-GATE-01..06 and the judge.call(retries=0) one-shot path (CR-1)."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from crews import egress, judge


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "egress"
    monkeypatch.setenv("CREWS_EGRESS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise OSError(f"test guard: outbound connection to {host} refused")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


class Stub:
    """A scripted loopback HTTP server, recording every request it receives."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({"auth": self.headers.get("Authorization"),
                                      "body": json.loads(self.rfile.read(length))})
                status, headers, body = stub.script.pop(0) if stub.script else (500, {}, {})
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stub]:
    s = Stub()
    monkeypatch.setenv("CREW_TYPESAFE_URL", s.url)
    monkeypatch.setenv("CREW_TYPESAFE_TIMEOUT", "0.5")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    yield s
    s.stop()


BODY = {"model": "jev-latest", "state": {"task": "refactor the billing adapter"},
        "questions": {"need": {"type": "choice", "instructions": "Which kind of work?",
                               "criteria": {"repeatable_task": "narrow", "difficult_review": "hard"}},
                     "divisible": {"type": "noul", "instructions": "Can it be split?"}}}


def gate(body: dict[str, Any] = BODY, transport: Any = None) -> dict[str, Any]:
    return egress.gated_call(body, purpose="test", requirement_ids=["TSI-GATE-01"], data_category="task_text",
                             includes_worker_output=False, decline_behavior="outage budget", transport=transport)


# TSI-GATE-01 -----------------------------------------------------------------------------------------------

def test_no_approval_returns_owner_gate_and_never_calls_transport() -> None:
    calls: list[Any] = []

    def spy(b: dict[str, Any]) -> dict[str, Any]:
        calls.append(b)
        return {"answers": {}}

    result = gate(transport=spy)
    assert result["state"] == "owner_gate"
    assert result["packet"]["attempt"] == 1
    assert calls == []


def test_owner_gate_packet_carries_the_mission_fields() -> None:
    result = gate()
    pkt = result["packet"]
    fp = egress.fingerprint(BODY)
    assert pkt["fingerprint"] == fp
    assert pkt["call_id"] == f"ts-{fp[:16]}-1"
    assert pkt["key_shown"] is False
    assert pkt["approve_command"] == f"crew approve {pkt['call_id']} --fingerprint {fp}"
    assert pkt["questions"]["need"]["options"] == ["difficult_review", "repeatable_task"]
    assert pkt["questions"]["divisible"]["options"] == []


# TSI-GATE-02 -----------------------------------------------------------------------------------------------

def test_approve_then_call_is_answered_then_same_body_gates_again_at_attempt_two() -> None:
    first = gate()
    pkt = first["packet"]
    egress.approve(pkt["call_id"], pkt["fingerprint"])

    answered = gate(transport=lambda b: {"answers": {"ok": True}})
    assert answered["state"] == "answered"
    assert answered["response"] == {"answers": {"ok": True}}

    second_gate = gate(transport=lambda b: (_ for _ in ()).throw(AssertionError("must not call transport again")))
    assert second_gate["state"] == "owner_gate"
    assert second_gate["packet"]["attempt"] == 2
    assert second_gate["packet"]["call_id"] != pkt["call_id"]


# TSI-GATE-03 -----------------------------------------------------------------------------------------------

def test_approve_refuses_unknown_call_id_and_mismatched_fingerprint() -> None:
    pkt = gate()["packet"]
    with pytest.raises(ValueError):
        egress.approve("ts-doesnotexist-1", pkt["fingerprint"])
    with pytest.raises(ValueError):
        egress.approve(pkt["call_id"], "0" * 64)


def test_approve_batch_refuses_wrong_sha256(tmp_path: Path) -> None:
    pkt = gate()["packet"]
    manifest = {"batch_id": "b1", "calls": [{"call_id": pkt["call_id"], "fingerprint": pkt["fingerprint"]}]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    real_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        egress.approve_batch(manifest_path, "0" * 64)
    assert real_sha != "0" * 64
    # unaffected: no approval was recorded by the refused batch, so the call stays gated
    still_gated = gate(transport=lambda b: (_ for _ in ()).throw(AssertionError("never approved")))
    assert still_gated["state"] == "owner_gate"


def test_approve_batch_refuses_manifest_edited_after_hashing(tmp_path: Path) -> None:
    pkt = gate()["packet"]
    manifest: dict[str, Any] = {"batch_id": "b1",
                                "calls": [{"call_id": pkt["call_id"], "fingerprint": pkt["fingerprint"]}]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    other = gate({**BODY, "state": {"task": "a different task entirely"}})["packet"]
    manifest["calls"].append({"call_id": other["call_id"], "fingerprint": other["fingerprint"]})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError):
        egress.approve_batch(manifest_path, original_sha)


def test_call_missing_from_approved_manifest_stays_gated(tmp_path: Path) -> None:
    pkt_a = gate()["packet"]
    other_body = {**BODY, "state": {"task": "a wholly separate task"}}
    pkt_b = gate(other_body)["packet"]

    manifest = {"batch_id": "b2", "calls": [{"call_id": pkt_a["call_id"], "fingerprint": pkt_a["fingerprint"]}]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    egress.approve_batch(manifest_path, sha)

    a_answered = gate(transport=lambda b: {"answers": {"a": True}})
    assert a_answered["state"] == "answered"

    b_gated = gate(other_body, transport=lambda b: (_ for _ in ()).throw(AssertionError("b was never approved")))
    assert b_gated["state"] == "owner_gate"
    assert b_gated["packet"]["call_id"] == pkt_b["call_id"]


# TSI-GATE-04 -----------------------------------------------------------------------------------------------

def test_judge_call_retries_zero_hits_a_429_stub_exactly_once(stub: Stub) -> None:
    stub.script = [(429, {"retry-after": "0"}, {})]
    with pytest.raises(judge.JudgeUnavailable):
        judge.call(BODY, retries=0)
    assert len(stub.requests) == 1


def test_gated_call_never_retries(stub: Stub) -> None:
    stub.script = [(429, {"retry-after": "0"}, {})]
    pkt = gate()["packet"]
    egress.approve(pkt["call_id"], pkt["fingerprint"])
    result = gate(transport=lambda b: judge.call(b, retries=0))
    assert result["state"] == "failed"
    assert len(stub.requests) == 1


# TSI-GATE-05 -----------------------------------------------------------------------------------------------

def test_worker_output_flag_reaches_the_packet() -> None:
    result = egress.gated_call(BODY, purpose="judge a hand back", requirement_ids=["TSI-GATE-05"],
                               data_category="worker_handback_text", includes_worker_output=True,
                               decline_behavior="stays UNJUDGED", transport=lambda b: {"answers": {}})
    assert result["packet"]["includes_worker_output"] is True


# TSI-GATE-06 -----------------------------------------------------------------------------------------------

def test_sentinel_key_never_appears_in_packet_ledger_or_stdio(stub: Stub, tmp_path: Path,
                                                              capsys: pytest.CaptureFixture[str],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "SENTINEL-EGRESS-9f3c7a21"
    key_file = tmp_path / "key"
    key_file.write_text(sentinel + "\n", encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY_FILE", str(key_file))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    stub.script = [(200, {"Content-Type": "application/json"}, {"answers": {}})]

    gated = gate()
    pkt = gated["packet"]
    egress.approve(pkt["call_id"], pkt["fingerprint"])
    answered = gate(transport=lambda b: judge.call(b, retries=0))
    assert answered["state"] == "answered"
    print(json.dumps({"gated_packet": pkt, "answered_result": answered}))
    out = capsys.readouterr()

    ledger_texts = [p.read_text(encoding="utf-8") for p in egress._dir().glob("*.jsonl")]

    assert stub.requests and stub.requests[0]["auth"] == f"Bearer {sentinel}"
    assert sentinel not in json.dumps(pkt)
    assert sentinel not in json.dumps(answered)
    assert sentinel not in out.out and sentinel not in out.err
    assert not any(sentinel in t for t in ledger_texts)


# concurrency: exactly one transport call per approval ------------------------------------------------------

def test_two_concurrent_consumers_of_one_approval_produce_exactly_one_transport_call() -> None:
    pkt = gate()["packet"]
    egress.approve(pkt["call_id"], pkt["fingerprint"])

    calls: list[float] = []
    calls_lock = threading.Lock()

    def slow_transport(b: dict[str, Any]) -> dict[str, Any]:
        with calls_lock:
            calls.append(time.monotonic())
        time.sleep(0.05)
        return {"answers": {}}

    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()

    def worker() -> None:
        r = gate(transport=slow_transport)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    states = sorted(r["state"] for r in results)
    assert states == ["answered", "owner_gate"]
