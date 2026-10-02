"""Tests for crews.record and the `crew record` CLI subcommand. All fixtures are synthetic and live under tmp_path."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from crews import record


def _line(message: dict) -> str:
    return json.dumps({"message": message})


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _handback_message(text: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "some preamble"},
            {"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}},
        ],
    }


def _assistant_text_message(*parts: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": p} for p in parts]}


def test_extract_handback_present(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        _line(_assistant_text_message("not the answer")),
        _line(_handback_message("the real report")),
    ])
    text, source = record.extract(transcript)
    assert text == "the real report"
    assert source == "handback"


def test_extract_handback_absent_falls_back_to_assistant_text(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        _line(_assistant_text_message("first")),
        _line(_assistant_text_message("second", " continued")),
        _line({"role": "user", "content": [{"type": "text", "text": "not assistant"}]}),
    ])
    text, source = record.extract(transcript)
    assert text == "second continued"
    assert source == "assistant_text"


def test_extract_several_handbacks_last_wins(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        _line(_handback_message("first handback")),
        _line(_assistant_text_message("noise in between")),
        _line(_handback_message("final handback")),
    ])
    text, source = record.extract(transcript)
    assert text == "final handback"
    assert source == "handback"


def test_extract_nothing_found_raises(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        _line({"role": "user", "content": [{"type": "text", "text": "hello"}]}),
        _line({"role": "assistant", "content": []}),
    ])
    with pytest.raises(record.RecordError):
        record.extract(transcript)


def test_extract_skips_non_json_line(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    lines = [
        "this is not json at all {{{",
        _line(_handback_message("survives the garbage line")),
    ]
    _write_jsonl(transcript, lines)
    text, source = record.extract(transcript)
    assert text == "survives the garbage line"
    assert source == "handback"


def test_write_refuses_existing_without_force(tmp_path: Path) -> None:
    out = tmp_path / "out" / "record.md"
    out.parent.mkdir(parents=True)
    out.write_text("old", encoding="utf-8")
    with pytest.raises(record.RecordError):
        record.write("new text", out, force=False)
    assert out.read_text(encoding="utf-8") == "old"


def test_write_allowed_with_force(tmp_path: Path) -> None:
    out = tmp_path / "out" / "record.md"
    out.parent.mkdir(parents=True)
    out.write_text("old", encoding="utf-8")
    written = record.write("new text", out, force=True)
    assert out.read_text(encoding="utf-8") == "new text"
    assert written == len("new text".encode("utf-8"))


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "record.md"
    written = record.write("hello", out, force=False)
    assert out.read_text(encoding="utf-8") == "hello"
    assert written == len(b"hello")


ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "crew.py"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(CREW), "record", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_cli_output_shape(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [_line(_handback_message("cli report body"))])
    out = tmp_path / "record.md"
    result = _run_cli(["--transcript", str(transcript), "--out", str(out)])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"out", "bytes", "source"}
    assert payload["source"] == "handback"
    assert payload["out"] == str(out)
    assert payload["bytes"] == len("cli report body".encode("utf-8"))
    assert "cli report body" not in result.stdout
    assert out.read_text(encoding="utf-8") == "cli report body"


def test_cli_refused_overwrite_without_force(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [_line(_handback_message("body"))])
    out = tmp_path / "record.md"
    out.write_text("existing", encoding="utf-8")
    result = _run_cli(["--transcript", str(transcript), "--out", str(out)])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "record refused" in result.stderr
    assert out.read_text(encoding="utf-8") == "existing"


def test_cli_overwrite_allowed_with_force(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [_line(_handback_message("body two"))])
    out = tmp_path / "record.md"
    out.write_text("existing", encoding="utf-8")
    result = _run_cli(["--transcript", str(transcript), "--out", str(out), "--force"])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "handback"
    assert out.read_text(encoding="utf-8") == "body two"


def test_cli_nothing_found_exit_one(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [_line({"role": "user", "content": [{"type": "text", "text": "hi"}]})])
    out = tmp_path / "record.md"
    result = _run_cli(["--transcript", str(transcript), "--out", str(out)])
    assert result.returncode == 1
    assert "record refused" in result.stderr
    assert not out.exists()
