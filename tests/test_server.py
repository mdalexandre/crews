"""Phase 7: the MCP server, driven only through a stdio client (mcp.client.stdio). Every key is a synthetic
sentinel and every judge call goes to a loopback stub."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

import hashlib  # noqa: E402
import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shlex  # noqa: E402
import site  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Awaitable, Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import get_default_environment, stdio_client  # noqa: E402

from crews import catalog as crews_catalog  # noqa: E402
from crews import cli as crews_cli  # noqa: E402
from crews import egress  # noqa: E402

# JEV-25: the shipped crews/catalog.json now carries an installed typesafe.need_band with real
# provenance, which crews.cli._resolve_need_band prefers unconditionally over any calibration
# directory file (crews/cli.py:191-203). test_server_secret_sentinel's stub confidence and its
# calibration-dir band below are both calibrated against this installed band's criteria hash and
# act threshold, aliased as crews_cli/crews_catalog to avoid colliding with the local cli() helper
# defined below, which drives crew.py as a subprocess.
_CAT = crews_catalog.load()
_NEED_CRITERIA_HASH = crews_cli._need_criteria_hash(_CAT)
_INSTALLED_ACT_THRESHOLD = ((_CAT.get("typesafe") or {}).get("need_band") or {}).get("act", {}).get("threshold")
# JEV-17: the same installed band also carries a real provenance.model, and gate_need now refuses to
# admit when a live response's own model differs from it (crews/need_gate.py). Stub reports this real
# model rather than a placeholder, since a real TypeSafe response reports the model that answered it.
_INSTALLED_NEED_MODEL = ((_CAT.get("typesafe") or {}).get("need_band") or {}).get("provenance", {}).get("model")

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
CREW = ROOT / "crew.py"
FIX = ROOT / "tests" / "fixtures"
TABLE = json.loads((FIX / "live_table.json").read_text(encoding="utf-8"))["rows"]
NAMES = ["rename_button", "find_mrr", "refactor_billing", "security_review", "storefront_hub", "caption_listings"]
TOOLS = {"crew_catalog", "crew_check", "crew_budget", "crew_plan"}
SENTINEL = "sk-synthetic-SENTINEL-7f3a9c-never-real"
CACHE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

Probe = Callable[[ClientSession], Awaitable[Any]]


def serve(probe: Probe, env: dict[str, str] | None = None, errlog: Path | None = None) -> Any:
    """Start server.py over stdio, complete the handshake, run the probe, and return what it returns."""
    async def main() -> Any:
        given = env or {}
        # keep the server's state out of the real ~/.crews unless the test points HOME or CREWS_HOME itself
        isolate = {"CREWS_HOME": os.environ["CREWS_HOME"]} if "CREWS_HOME" in os.environ and not (
            {"HOME", "CREWS_HOME"} & set(given)) else {}
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                       env={**get_default_environment(), **isolate, **given})
        with open(errlog or os.devnull, "w", encoding="utf-8") as err:
            async with stdio_client(params, errlog=err) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return await probe(s)
    return anyio.run(main)


async def call(s: ClientSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await s.call_tool(name, args)
    assert not res.isError, res
    envelope: dict[str, Any] = json.loads(res.content[0].text)  # type: ignore[union-attr]
    return envelope


def calls(batch: list[tuple[str, dict[str, Any]]], **kw: Any) -> list[dict[str, Any]]:
    async def probe(s: ClientSession) -> list[dict[str, Any]]:
        return [await call(s, name, args) for name, args in batch]
    result: list[dict[str, Any]] = serve(probe, **kw)
    return result


def cli(*args: str) -> tuple[int, Any]:
    r = subprocess.run([sys.executable, "-X", "utf8", str(CREW), *args], capture_output=True, text=True, timeout=60)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def manifest(root: Path) -> dict[str, str]:
    if root.is_file():
        return {str(root): hashlib.sha256(root.read_bytes()).hexdigest()}
    out: dict[str, str] = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in CACHE_DIRS]
        for f in fns:
            p = Path(dp) / f
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def answers(name: str) -> dict[str, Any]:
    row: dict[str, Any] = TABLE[NAMES.index(name)]["answers"]
    return row


def roles(name: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads((FIX / "roles" / f"{name}.json").read_text(encoding="utf-8"))
    return spec


EIGHT = {"roles": {"listing-caption-writer": {"workers": 7}}}


@pytest.fixture(scope="module")
def agents(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("agents")
    assert cli("install", "--agents-dir", str(d))[0] == 0
    return d


def test_server_tool_surface() -> None:
    async def probe(s: ClientSession) -> Any:
        return (await s.list_tools()).tools
    tools = serve(probe)
    assert {t.name for t in tools} == TOOLS and len(tools) == 4
    assert all(t.inputSchema.get("type") == "object" for t in tools)


def test_server_parity_with_the_cli(tmp_path: Path, agents: Path) -> None:
    batch: list[tuple[str, dict[str, Any]]] = [("crew_catalog", {}), ("crew_check", {"agents_dir": str(agents)}),
                                               ("crew_budget", {"outage": True, "run_dir": str(tmp_path / "o")})]
    twins: list[Any] = [cli("list", "--json")[1], cli("check", "--agents-dir", str(agents))[1],
                        cli("budget", "--outage")[1]]
    for name in NAMES:
        ans = tmp_path / f"{name}.answers.json"
        ans.write_text(json.dumps(answers(name)), encoding="utf-8")
        run = tmp_path / name
        code, plan = cli("plan", "--roles", str(FIX / "roles" / f"{name}.json"), "--answers", str(ans),
                         "--run-dir", str(run))
        assert code == 0
        batch += [("crew_budget", {"answers": answers(name), "run_dir": str(tmp_path / f"b-{name}")}),
                  ("crew_plan", {"roles": roles(name), "answers": answers(name), "run_dir": str(run),
                                 "verbose": True})]  # the CLI prints the full view; compact is the default
        twins += [cli("budget", "--answers", str(ans))[1], plan]
    envelopes = calls(batch)
    assert len(envelopes) == len(twins)
    # crew_budget and crew_plan now default judge/skills/brief_check "live" (owner directive 2026-09-22);
    # on this stock test machine (no TYPESAFE_API_KEY_FILE) that degrades to "off" with a Q9 warning, so
    # their envelopes carry a warning even though their CLI twins (--judge off by argparse default) do not.
    # The digest comparison below is over "output" only, which the warning never touches.
    for (tool, _), env, twin in zip(batch, envelopes, twins):
        expected = "COMPLETED_WITH_WARNINGS" if tool in ("crew_budget", "crew_plan") else "COMPLETED"
        assert env["status"] == expected, (tool, env["errors"], env["warnings"])
        assert digest(env["output"]) == digest(twin), tool


def test_server_never_raises(tmp_path: Path) -> None:
    unnamed = roles("rename_button")
    unnamed.pop("task")
    generic = roles("security_review")
    generic["roles"][0]["name"] = "verifier"
    bad_need = {**answers("rename_button"), "need": {**answers("rename_button")["need"], "choice": "no_such_need"}}
    batch: list[tuple[str, dict[str, Any]]] = [
        ("crew_plan", {"roles": "{not json", "outage": True, "run_dir": str(tmp_path / "a")}),
        ("crew_plan", {"roles": unnamed, "answers": answers("rename_button"), "run_dir": str(tmp_path / "b")}),
        # A bare judge="off" call with nothing declared refuses (crews.cli.resolve_task_answers): "give
        # declared answers, the outage flag, or judge live". A bare judge="live" call (the new default)
        # with nothing declared no longer belongs here: on this key-file-less test machine it falls back
        # to the outage-default budget instead of erroring (A4; see test_server_default_live_bare_call_
        # falls_back_to_outage_below).
        ("crew_budget", {"judge": "off", "run_dir": str(tmp_path / "c")}),
        ("crew_budget", {"answers": bad_need, "run_dir": str(tmp_path / "d")}),
        ("crew_plan", {"roles": generic, "answers": answers("security_review"), "run_dir": str(tmp_path / "e")}),
        ("crew_plan", {"roles": [1, 2], "outage": True, "run_dir": str(tmp_path / "f")}),
    ]
    for (tool, _), env in zip(batch, calls(batch)):
        assert env["status"] in ("BLOCKED", "FAILED") and env["errors"], (tool, env)
    assert "verifier" in calls([batch[4]])[0]["errors"][0]


def test_server_default_live_bare_call_falls_back_to_outage(tmp_path: Path) -> None:
    """A4: a bare crew_budget call (no answers, no outage, no task; only its new "live" default) on this
    key-file-less test machine falls back to the outage-default budget with a Q9 warning, COMPLETED_WITH_
    WARNINGS, never BLOCKED: server.crew_budget only forces outage once _live_gate has already downgraded
    the mode, and only when no declared answers stand to use instead."""
    env = calls([("crew_budget", {"run_dir": str(tmp_path / "bare")})])[0]
    outage = cli("budget", "--outage")[1]
    assert env["status"] == "COMPLETED_WITH_WARNINGS" and any("SRD Q9" in w for w in env["warnings"])
    assert {k: env["output"][k] for k in ("budget", "parallel_cells")} == {k: outage[k] for k in
                                                                           ("budget", "parallel_cells")}


def test_server_writes_stay_in_the_run_dir(tmp_path: Path, agents: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    watched = [ROOT]
    before = [manifest(p) for p in watched if p.exists()]
    run = tmp_path / "explicit"
    batch: list[tuple[str, dict[str, Any]]] = [
        ("crew_budget", {"answers": answers("find_mrr"), "run_dir": str(run)}),
        ("crew_plan", {"roles": roles("find_mrr"), "answers": answers("find_mrr"), "run_dir": str(run)}),
        ("crew_budget", {"answers": answers("find_mrr")}),
        ("crew_plan", {"roles": roles("find_mrr"), "answers": answers("find_mrr")}),
    ]
    envelopes = calls(batch, env={"HOME": str(home), "PYTHONUSERBASE": site.getuserbase()})
    assert [p for p in watched if p.exists()] and [manifest(p) for p in watched if p.exists()] == before
    run_dirs = set()
    # crew_budget/crew_plan default judge/skills/brief_check "live" and this test machine carries no
    # TYPESAFE_API_KEY_FILE, so those four envelopes carry a Q9 warning.
    expected_statuses = ["COMPLETED_WITH_WARNINGS"] * 4
    for env, expected in zip(envelopes, expected_statuses):
        assert env["status"] == expected, env["errors"]
        named = Path(env["evidence"][0]["run_dir"])
        run_dirs.add(named)
        for item in env["evidence"][1:]:
            assert Path(item["written"]).is_relative_to(named)
    assert run in run_dirs
    defaults = [Path(e["evidence"][0]["run_dir"]) for e in envelopes[2:]]
    assert all(d.parent == home / ".crews" / "runs" and d.name == e["execution_id"]
               for d, e in zip(defaults, envelopes[2:]))
    # the plan index (OSS-FIX-1) is the one other thing a plan writes, under <crews home>/plans/
    index_dir = home / ".crews" / "plans"
    strays = [p for p in home.rglob("*") if p.is_file() and not any(p.is_relative_to(d) for d in defaults)
              and not p.is_relative_to(index_dir)]
    assert strays == []
    assert (index_dir / "index.jsonl").is_file()
    assert {p.name for p in run.iterdir()} >= {"budget.json", "plan.json", "seats.json"}


def test_server_plan_output_fits(tmp_path: Path) -> None:
    args = {"roles": roles("caption_listings"), "answers": answers("caption_listings"), "run_dir": str(tmp_path),
            "allow": 8, "force": EIGHT}

    async def probe(s: ClientSession) -> tuple[int, dict[str, Any]]:
        res = await s.call_tool("crew_plan", args)
        return len(res.model_dump_json().encode("utf-8")), json.loads(res.content[0].text)  # type: ignore[union-attr]
    size, env = serve(probe)
    # No TYPESAFE_API_KEY_FILE on this test machine: judge/skills/brief_check default "live" and downgrade
    # to "off" with a Q9 warning (server._live_gate), so this crew_plan call is COMPLETED_WITH_WARNINGS.
    assert env["status"] == "COMPLETED_WITH_WARNINGS" and env["output"]["cells"] == 8
    # brief contract v2 (SRD spec V2-2) renders a typed XML envelope per cell, larger than the v1 free prose brief.
    assert size <= 30_000


# JEV-25: clears the installed catalog band's act threshold (0.94 as measured 2026-09-22; see
# _INSTALLED_ACT_THRESHOLD above) with a fixed margin, so the echoed answer reaches admit through
# whichever band _resolve_need_band actually selects, rather than a value tuned to a threshold this
# test's calibration_dir fixture cannot override once an installed band exists.
_STUB_CONFIDENCE = min(0.99, (_INSTALLED_ACT_THRESHOLD or 0.85) + 0.03)


class Stub(http.server.BaseHTTPRequestHandler):
    """Echoes the Authorization header into every string field an answer can carry that this test
    checks for a leak, except "model": that field is read by crews.need_gate's JEV-17 model guard
    and compared against the installed band's real provenance.model, so it reports the real
    installed model (_INSTALLED_NEED_MODEL) the way an actual TypeSafe response would, rather than
    the auth header, which would make the guard correctly (and, for this test, unintentionally)
    refuse the answer as a model mismatch, and this stub exists to prove that Request 1 actually
    reaches "admit" end to end. The "model" field is not therefore dropped from leak coverage: a
    second, dedicated stub (ModelLeakStub) and test (test_server_model_field_secret_scrubbed
    below) puts the auth header in "model" and nowhere else, so a scrub that stopped covering that
    one field is caught there without reintroducing the tension that forced this stub's real model
    in the first place: one response cannot both match the band (to prove admit) and carry a
    sentinel in the field the band checks (to prove the leak scrub)."""
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        auth = self.headers.get("authorization") or ""
        Stub.seen.append(auth)
        body = {"model": _INSTALLED_NEED_MODEL, "usage": {"echo": auth},
                "answers": {"need": {"choice": "everyday_implementation", "confidence": _STUB_CONFIDENCE,
                                     "probabilities": {"everyday_implementation": _STUB_CONFIDENCE}},
                            "difficulty": {"score": 1.0}, "divisible": {"noul": 0.2},
                            "needs_verifier": {"noul": 0.8}}}
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture()
def stub() -> Iterator[str]:
    Stub.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/systemone"
    srv.shutdown()


def test_server_secret_sentinel(tmp_path: Path, stub: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # TSI-GATE-01: every live judge call, including crew_budget's, is now gated through crews/egress.py's
    # owner approval ledger. Point the ledger, the need_gate calibration dir, and the audit dir at this
    # test's tmp_path (both for the subprocess server and for the in-process egress.approve() call below),
    # the same directories tests/test_typesafe_wiring.py's egress_env fixture uses.
    egress_dir = str(tmp_path / "egress")
    calibration_dir = tmp_path / "calibration"
    audit_dir = str(tmp_path / "audit")
    monkeypatch.setenv("CREWS_EGRESS_DIR", egress_dir)
    monkeypatch.setenv("CREWS_CALIBRATION_DIR", str(calibration_dir))
    monkeypatch.setenv("CREWS_AUDIT_DIR", audit_dir)
    gate_env = {"CREWS_EGRESS_DIR": egress_dir, "CREWS_CALIBRATION_DIR": str(calibration_dir),
               "CREWS_AUDIT_DIR": audit_dir}
    # A measured confidence band (the tests/test_typesafe_wiring.py _write_bands shape), carrying
    # provenance.criteria_hash for the criteria this same catalog gates against, so this band would be
    # genuinely calibrated (not "uncalibrated" for want of a criteria hash) if it were ever consulted.
    # JEV-25: crews/catalog.json now ships an installed typesafe.need_band with real provenance, which
    # crews.cli._resolve_need_band (crews/cli.py:191-203) prefers unconditionally over this calibration
    # directory file; the subprocess server always loads the real installed catalog, so this test cannot
    # swap it out the way tests/test_typesafe_wiring.py's CAT_NO_INSTALLED_NEED_BAND does in process. The
    # decision that actually reaches "admit" here comes from the installed band, which is why the stub
    # answers with _STUB_CONFIDENCE (above the installed band's act threshold) rather than this file's
    # own threshold. This file is written anyway so the calibration-dir fallback path stays exercised and
    # correctly provenanced for the day the installed band is absent.
    calibration_dir.mkdir(parents=True, exist_ok=True)
    (calibration_dir / "bands.json").write_text(json.dumps({
        "act": {"status": "MEASURED", "threshold": 0.85, "n": 20, "k": 19, "precision": 0.95, "wilson_lower": 0.86},
        "confirm": {"status": "MEASURED", "threshold": 0.6, "n": 20, "k": 15, "precision": 0.75, "wilson_lower": 0.62},
        "provenance": {"criteria_hash": _NEED_CRITERIA_HASH, "catalog_version": _CAT.get("version"),
                       "n_observations": 20, "observations_path": "/tmp/does-not-matter/observations.jsonl",
                       "measured_at": "2026-09-22T19:00:00+00:00"},
    }), encoding="utf-8")

    keyfile = tmp_path / "key"
    keyfile.write_text(SENTINEL + "\n", encoding="utf-8")
    err = tmp_path / "stderr.log"
    run = tmp_path / "live"
    live = {"judge": "live", "task": "Rename the Save button on the checkout form", "run_dir": str(run)}
    live_env = {**gate_env, "TYPESAFE_API_KEY_FILE": str(keyfile), "CREW_TYPESAFE_URL": stub}
    gate = calls([("crew_budget", live)], env=live_env)[0]
    assert gate["status"] == "COMPLETED_WITH_WARNINGS" and gate["output"]["judge"]["status"] == "owner_gate"
    assert Stub.seen == []
    for pkt in gate["output"]["owner_gates"]:
        egress.approve(pkt["call_id"], pkt["fingerprint"])
    env = calls([("crew_budget", live)], env=live_env, errlog=err)[0]
    assert Stub.seen and all(SENTINEL in a for a in Stub.seen)
    assert env["status"] == "COMPLETED" and env["output"]["judge"]["status"] == "ok"
    leaks = [json.dumps(gate), json.dumps(env), err.read_text(encoding="utf-8")] + [
        p.read_text(encoding="utf-8") for p in run.rglob("*") if p.is_file()]
    assert not any(SENTINEL in text for text in leaks)
    absent = calls([("crew_budget", {**live, "run_dir": str(tmp_path / "absent")})],
                   env={**gate_env, "TYPESAFE_API_KEY_FILE": str(tmp_path / "missing"), "CREW_TYPESAFE_URL": stub})[0]
    unset = calls([("crew_budget", {**live, "run_dir": str(tmp_path / "unset")})],
                  env={**gate_env, "TYPESAFE_API_KEY": SENTINEL, "CREW_TYPESAFE_URL": stub})[0]
    outage = cli("budget", "--outage")[1]
    for e in (absent, unset):
        assert e["status"] == "COMPLETED_WITH_WARNINGS" and e["warnings"]
        assert {k: e["output"][k] for k in ("budget", "parallel_cells")} == {k: outage[k] for k in
                                                                             ("budget", "parallel_cells")}
    assert "SRD Q9" in unset["warnings"][0] and len(Stub.seen) == 1
    assert SENTINEL not in json.dumps([absent, unset])


class ModelLeakStub(http.server.BaseHTTPRequestHandler):
    """Isolates leak coverage on the "model" field alone (JEV-17: crews/judge.py:192 meta()["model"] ->
    crews/cli.py's answering_model, which lands in a written run file and the envelope output), the one
    field Stub above cannot carry a sentinel in without also making its answer fail the JEV-17 model
    guard and never reach admit (see the Stub docstring). This stub puts the auth header in "model" and
    only "model"; everything else is a safe placeholder. It proves nothing about the admit path and
    everything about the scrub: crews.judge.call's _scrub runs over the whole response before anything
    parses it, and if it ever stopped covering the "model" key, this is the test that would catch it,
    where test_server_secret_sentinel above no longer can."""
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        auth = self.headers.get("authorization") or ""
        ModelLeakStub.seen.append(auth)
        body = {"model": auth, "usage": {},
                "answers": {"need": {"choice": "everyday_implementation", "confidence": 0.5,
                                     "probabilities": {"everyday_implementation": 0.5}},
                            "difficulty": {"score": 1.0}, "divisible": {"noul": 0.2},
                            "needs_verifier": {"noul": 0.8}}}
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture()
def model_leak_stub() -> Iterator[str]:
    ModelLeakStub.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ModelLeakStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/systemone"
    srv.shutdown()


def test_server_model_field_secret_scrubbed(tmp_path: Path, model_leak_stub: str,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """crew task answering-model-absence-and-leak-coverage-builder, A4/A5: restores the secret leak
    check on the "model" field that test_server_secret_sentinel's stub can no longer carry once it had
    to report the real installed model to reach admit (see both stubs' docstrings above). This test does
    not need admit; it only needs the sentinel to travel the real Request 1 path (crews.judge.call's
    _scrub, then crews.cli._live_task_answers, then judge.meta, then result["judge"]) sitting in "model"
    and nowhere else, and to be absent from the envelope, stderr, and every written run file. A local
    copy of crews/judge.py with the "model" key excluded from _scrub's dict branch was run against this
    exact test and it failed (the sentinel then reached env["output"]["judge"]["model"] and every leak
    surface below); with the real, unmodified scrub in place it passes."""
    egress_dir = str(tmp_path / "egress")
    audit_dir = str(tmp_path / "audit")
    monkeypatch.setenv("CREWS_EGRESS_DIR", egress_dir)
    monkeypatch.setenv("CREWS_AUDIT_DIR", audit_dir)
    gate_env = {"CREWS_EGRESS_DIR": egress_dir, "CREWS_AUDIT_DIR": audit_dir}
    keyfile = tmp_path / "key"
    keyfile.write_text(SENTINEL + "\n", encoding="utf-8")
    err = tmp_path / "stderr.log"
    run = tmp_path / "live"
    live = {"judge": "live", "task": "Rename the Save button on the checkout form", "run_dir": str(run)}
    live_env = {**gate_env, "TYPESAFE_API_KEY_FILE": str(keyfile), "CREW_TYPESAFE_URL": model_leak_stub}
    gate = calls([("crew_budget", live)], env=live_env)[0]
    assert gate["status"] == "COMPLETED_WITH_WARNINGS" and gate["output"]["judge"]["status"] == "owner_gate"
    assert ModelLeakStub.seen == []
    for pkt in gate["output"]["owner_gates"]:
        egress.approve(pkt["call_id"], pkt["fingerprint"])
    env = calls([("crew_budget", live)], env=live_env, errlog=err)[0]
    assert ModelLeakStub.seen and all(SENTINEL in a for a in ModelLeakStub.seen)
    # A scrubbed "model" no longer equals the installed band's real provenance.model, so JEV-17's own
    # guard correctly refuses to admit this answer (it cannot verify who answered); that is a side
    # effect of choosing a field the guard reads, not a defect of this test, so the envelope carries a
    # warning rather than a bare "COMPLETED", and judge.status stays "ok" since the call itself succeeded.
    assert env["status"] == "COMPLETED_WITH_WARNINGS" and env["output"]["judge"]["status"] == "ok"
    assert env["output"]["judge"]["need_gate"]["decision"] == "uncalibrated"
    # The sentinel sat in "model" and nowhere else on the wire, so a redacted "model" specifically
    # proves the scrub covered that field, not merely some other field this response also carried.
    model_out = env["output"]["judge"].get("model") or ""
    assert model_out != ModelLeakStub.seen[-1] and "[redacted]" in model_out and SENTINEL not in model_out
    leaks = [json.dumps(gate), json.dumps(env), err.read_text(encoding="utf-8")] + [
        p.read_text(encoding="utf-8") for p in run.rglob("*") if p.is_file()]
    assert not any(SENTINEL in text for text in leaks)


class AbsentModelStub(http.server.BaseHTTPRequestHandler):
    """crew task answering-model-absence-and-leak-coverage-builder, A1: proves DEFECT ONE is closed at
    the real call site (crews/cli.py:256, crews.need_gate.gate_need called through crews.cli.
    _live_task_answers), not only through gate_need() directly. Reports a confidence safely above the
    installed act threshold and NO "model" key at all, exactly what an answering endpoint that omits the
    field looks like on the wire; crews.judge.meta(response)["model"] reads that the same way it reads
    an explicit null (both are response.get("model") returning None), so this one stub stands in for
    either shape. Before this crew task's fix, crews.need_gate.gate_need's `if model is not None:` guard
    treated that None exactly like "the caller never asked for the check" and let this confidence admit
    on threshold alone, against a real installed band that names the model it was measured for."""
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length") or 0))
        auth = self.headers.get("authorization") or ""
        AbsentModelStub.seen.append(auth)
        body = {"usage": {},
                "answers": {"need": {"choice": "everyday_implementation", "confidence": _STUB_CONFIDENCE,
                                     "probabilities": {"everyday_implementation": _STUB_CONFIDENCE}},
                            "difficulty": {"score": 1.0}, "divisible": {"noul": 0.2},
                            "needs_verifier": {"noul": 0.8}}}
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture()
def absent_model_stub() -> Iterator[str]:
    AbsentModelStub.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AbsentModelStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/systemone"
    srv.shutdown()


def test_server_no_admit_when_answering_model_absent(tmp_path: Path, absent_model_stub: str,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: a live response carrying no "model" key produces a non acting need_gate decision when the
    band records a model, proven at crews/cli.py:256's real _live_task_answers call site through the
    MCP server rather than only through crews.need_gate.gate_need in isolation. The stub's confidence
    clears the installed band's act threshold (_STUB_CONFIDENCE, defined above for the Stub class), so
    the only thing standing between this answer and "admit" is the JEV-17 model check; it must still
    refuse, and the declared "need" answer must never be treated as the live admitted one."""
    egress_dir = str(tmp_path / "egress")
    audit_dir = str(tmp_path / "audit")
    monkeypatch.setenv("CREWS_EGRESS_DIR", egress_dir)
    monkeypatch.setenv("CREWS_AUDIT_DIR", audit_dir)
    gate_env = {"CREWS_EGRESS_DIR": egress_dir, "CREWS_AUDIT_DIR": audit_dir}
    keyfile = tmp_path / "key"
    keyfile.write_text(SENTINEL + "\n", encoding="utf-8")
    run = tmp_path / "live"
    live = {"judge": "live", "task": "Rename the Save button on the checkout form", "run_dir": str(run)}
    live_env = {**gate_env, "TYPESAFE_API_KEY_FILE": str(keyfile), "CREW_TYPESAFE_URL": absent_model_stub}
    gate = calls([("crew_budget", live)], env=live_env)[0]
    assert gate["status"] == "COMPLETED_WITH_WARNINGS" and gate["output"]["judge"]["status"] == "owner_gate"
    for pkt in gate["output"]["owner_gates"]:
        egress.approve(pkt["call_id"], pkt["fingerprint"])
    env = calls([("crew_budget", live)], env=live_env)[0]
    assert AbsentModelStub.seen and env["output"]["judge"]["status"] == "ok"
    need_gate_result = env["output"]["judge"]["need_gate"]
    assert need_gate_result["decision"] == "uncalibrated"
    assert "model" in need_gate_result["reason"] and "missing" in need_gate_result["reason"]
    assert need_gate_result["threshold_act"] is None and need_gate_result["threshold_confirm"] is None
    # The refusal must actually withhold routing, not merely be recorded alongside an admitted answer:
    # crews.cli._live_task_answers returns None (never the parsed live answer) on anything but "admit"
    # or "confirm", so budget.compute falls back to its outage default (no declared answers were given
    # here either), never the stub's own everyday_implementation choice at _STUB_CONFIDENCE.
    assert "confirm_need" not in env["output"]
    assert "need_gate uncalibrated" in " ".join(env["output"]["notes"])


def test_server_injection_safe(tmp_path: Path) -> None:
    hostile = roles("rename_button")
    hostile["task"] += (f" \"quoted\" 'single' $(touch {tmp_path}/pwned) `touch {tmp_path}/pwned2`"
                        "\nsecond line; rm nothing")
    env = calls([("crew_plan", {"roles": hostile, "answers": answers("rename_button"), "run_dir": str(tmp_path)})])[0]
    # No TYPESAFE_API_KEY_FILE on this test machine: judge/skills/brief_check default "live" and downgrade
    # to "off" with a Q9 warning (server._live_gate).
    assert env["status"] == "COMPLETED_WITH_WARNINGS", env["errors"]
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    argvs = [c["headless_argv"] for w in plan["waves"] for c in w["calls"]]
    assert argvs and all(shlex.split(shlex.join(a)) == a for a in argvs)
    assert any(hostile["task"] in part for a in argvs for part in a)
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()
    sources = {p.name: p.read_text(encoding="utf-8") for p in [SERVER, *(ROOT / "crews").glob("*.py")]}
    assert not any("shell=True" in t or "os.system" in t or "os.popen" in t for t in sources.values())
    assert sorted(n for n, t in sources.items() if "import subprocess" in t) == []


def _server_pid() -> int:
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            cmd = (stat.parent / "cmdline").read_bytes()
        except OSError:
            continue
        if int(fields[1]) == os.getpid() and str(SERVER).encode() in cmd:
            return int(stat.parent.name)
    raise AssertionError("server process not found")


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")
def test_server_is_cheap(tmp_path: Path) -> None:
    args = {"roles": roles("caption_listings"), "answers": answers("caption_listings"), "run_dir": str(tmp_path),
            "allow": 8, "force": EIGHT}

    async def probe(s: ClientSession) -> tuple[int, float, dict[str, Any]]:
        status = (Path("/proc") / str(_server_pid()) / "status").read_text()
        rss_kb = int(next(line for line in status.splitlines() if line.startswith("VmRSS:")).split()[1])
        t0 = time.perf_counter()
        env = await call(s, "crew_plan", args)
        return rss_kb, time.perf_counter() - t0, env
    rss_kb, seconds, env = serve(probe)
    assert env["output"]["cells"] == 8
    assert rss_kb < 150 * 1024, rss_kb
    assert seconds < 2.0, seconds
