"""tests/test_usage_accounting.py: token accounting on the PRODUCTION `crew plan --judge live` path
(mission prompt phase 9 step 8, A8).

crews/judge.py's meta(response) already returns {model, usage} from every TypeSafe response. The
calibration path already persists it (crews.bands.record_observation, kind "need"/"need_role"). The
production plan path (crews.cli.core_plan) persisted request 2's usage already (JEV-14,
request2_usage.json) but never request 1's, so a real owner-approved production call's spend for
request 1 was unrecoverable once the process exited. This module drives a stubbed live crew plan run
through both requests and asserts the usage for each is readable from the run directory afterwards,
in the same {"answering_model", "usage"} shape request2_usage.json already used."""

from __future__ import annotations

import copy
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from crews import catalog, cli, egress

CAT = catalog.load()

PRODUCE_ROLE = {
    "name": "usage-widget-builder", "kind": "produce",
    "mission": "Build a small reporting widget for the usage accounting test",
    "deliverable": "widget.md", "scope": ["src/widgets/UsageWidget.tsx"],
    "acceptance": ["the widget renders with accessible labels"],
    "answers": {"need": "repeatable_task", "difficulty": 0.2, "divisible": 0.1, "specific": 0.9},
}
CHECK_ROLE = {
    "name": "usage-widget-checker", "kind": "check",
    "mission": "Check the usage widget against its acceptance criteria",
    "deliverable": "check.md", "criteria": ["the widget renders with accessible labels"],
    "answers": {"need": "mechanical_verification", "difficulty": 0.2, "divisible": 0.0},
}
SPEC = {"task": "build a usage accounting test widget", "roles": [PRODUCE_ROLE, CHECK_ROLE]}


@pytest.fixture(autouse=True)
def egress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREWS_EGRESS_DIR", str(tmp_path / "egress"))
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(tmp_path / "calibration"))
    monkeypatch.setenv("CREWS_AUDIT_DIR", str(tmp_path / "audit"))


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
    """A scripted loopback HTTP server, recording every request it receives (pattern copied from
    tests/test_egress.py / tests/test_typesafe_wiring.py)."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({"body": json.loads(self.rfile.read(length))})
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
    monkeypatch.setenv("CREW_TYPESAFE_TIMEOUT", "1.0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    yield s
    s.stop()


REQ1_MODEL = "jev-req1-usage-test"
REQ1_USAGE = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
REQ2_MODEL = "jev-req2-usage-test"
REQ2_USAGE = {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}


def _req1_response() -> dict[str, Any]:
    return {"model": REQ1_MODEL, "usage": REQ1_USAGE,
            "answers": {"need": {"choice": "everyday_implementation", "confidence": 0.5,
                                 "probabilities": {"everyday_implementation": 0.5}},
                       "difficulty": {"score": 1.0}, "divisible": {"noul": 0.2},
                       "needs_verifier": {"noul": 0.8}}}


def _req2_response() -> dict[str, Any]:
    def one(i: int, need_choice: str) -> dict[str, Any]:
        return {f"need.{i}": {"choice": need_choice, "confidence": 0.5, "probabilities": {}},
                f"difficulty.{i}": {"score": 1.0}, f"divisible.{i}": {"noul": 0.2}, f"specific.{i}": {"noul": 0.5}}
    answers: dict[str, Any] = {}
    answers.update(one(0, "repeatable_task"))
    answers.update(one(1, "mechanical_verification"))
    return {"model": REQ2_MODEL, "usage": REQ2_USAGE, "answers": answers}


def _approve_every_gate(view: dict[str, Any]) -> None:
    for pkt in view.get("owner_gates") or []:
        egress.approve(pkt["call_id"], pkt["fingerprint"])


# --- A8 -------------------------------------------------------------------------------------------------------

def test_a8_live_plan_persists_request1_and_request2_usage(stub: Stub, tmp_path: Path) -> None:
    gate_dir = tmp_path / "gate-run"
    gated = cli.core_plan(CAT, spec=copy.deepcopy(SPEC), answers=None, outage=False, judge_mode="live",
                          run_dir=str(gate_dir), allow=2, force=None)
    assert gated.get("owner_gates"), "both request 1 and request 2 must gate with no prior approval"
    assert stub.requests == []
    _approve_every_gate(gated)

    stub.script = [(200, {}, _req1_response()), (200, {}, _req2_response())]
    run_dir = tmp_path / "live-run"
    view = cli.core_plan(CAT, spec=copy.deepcopy(SPEC), answers=None, outage=False, judge_mode="live",
                         run_dir=str(run_dir), allow=2, force=None)
    assert len(stub.requests) == 2

    req1_file = run_dir / "request1_usage.json"
    req2_file = run_dir / "request2_usage.json"
    assert req1_file.exists(), "request 1 usage must survive past the process that printed the view"
    assert req2_file.exists(), "request 2 usage must survive past the process that printed the view"

    req1_usage = json.loads(req1_file.read_text(encoding="utf-8"))
    req2_usage = json.loads(req2_file.read_text(encoding="utf-8"))
    assert req1_usage == {"answering_model": REQ1_MODEL, "usage": REQ1_USAGE}
    assert req2_usage == {"answering_model": REQ2_MODEL, "usage": REQ2_USAGE}

    # the same shape the calibration path already uses for a "need" observation's usage fields
    assert set(req1_usage) == {"answering_model", "usage"}
    assert set(req2_usage) == {"answering_model", "usage"}

    assert view["typesafe"]["task_judge"]["usage_file"] == "request1_usage.json"
    assert view["typesafe"]["task_judge"]["model"] == REQ1_MODEL
    assert view["typesafe"]["task_judge"]["usage"] == REQ1_USAGE
    assert view["typesafe"]["role_judge"]["usage_file"] == "request2_usage.json"
    assert view["typesafe"]["role_judge"]["model"] == REQ2_MODEL
    assert view["typesafe"]["role_judge"]["usage"] == REQ2_USAGE


def test_a8_owner_gate_writes_no_usage_file_before_approval(stub: Stub, tmp_path: Path) -> None:
    """No approval was ever spent, so there is no answered call and nothing to account for yet
    (mirrors request2_usage.json's own owner_gate behavior, JEV-14)."""
    run_dir = tmp_path / "gate-only"
    view = cli.core_plan(CAT, spec=copy.deepcopy(SPEC), answers=None, outage=False, judge_mode="live",
                         run_dir=str(run_dir), allow=2, force=None)
    assert view.get("owner_gates")
    assert stub.requests == []
    assert not (run_dir / "request1_usage.json").exists()
    assert not (run_dir / "request2_usage.json").exists()


def test_a8_off_mode_writes_no_usage_file_and_touches_no_network(stub: Stub, tmp_path: Path) -> None:
    run_dir = tmp_path / "off-run"
    answers = {"need": {"choice": "repeatable_task", "confidence": 0.9,
                        "probabilities": {"repeatable_task": 0.9}},
              "divisible": {"noul": 0.1}, "needs_verifier": {"noul": 0.2}, "difficulty": {"score": 0.5}}
    view = cli.core_plan(CAT, spec=copy.deepcopy(SPEC), answers=answers, outage=False, judge_mode="off",
                         run_dir=str(run_dir), allow=2, force=None)
    assert "typesafe" not in view
    assert stub.requests == []
    assert not (run_dir / "request1_usage.json").exists()
    assert not (run_dir / "request2_usage.json").exists()
