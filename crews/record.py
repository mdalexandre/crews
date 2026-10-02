"""crew record: extract a subagent hand-back from a Claude Code transcript, and write it as a record file.

Interface
    extract(transcript) -> (text, source)   text is the extracted hand-back or fallback text; source is
                                             "handback" or "assistant_text". Raises RecordError when nothing
                                             is found in the transcript.
    write(text, out, force) -> int          writes text to out atomically (temp file plus os.replace), creating
                                             parent directories as needed; returns the number of bytes written.
                                             Raises RecordError when out already exists and force is falsy.

The transcript is a Claude Code subagent JSONL file: one JSON object per line, each carrying an optional
"message" dict with a "content" list of blocks. A line that is not valid JSON is skipped, not refused.

extract() prefers the LAST tool_use block named "SubagentHandback" across every line, reading its
input["message"] as the hand-back text (source "handback"). When no such block exists, it falls back to the
LAST assistant message that carries any text blocks, joining their "text" fields (source "assistant_text").
When neither exists, extract() raises RecordError.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class RecordError(Exception):
    """The transcript held nothing to record, or the record file already exists without --force."""


def _iter_messages(transcript: Path) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if isinstance(msg, dict):
            messages.append(msg)
    return messages


def extract(transcript: Path) -> tuple[str, str]:
    messages = _iter_messages(transcript)

    handback_text: str | None = None
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                block_input = block.get("input")
                if isinstance(block_input, dict) and isinstance(block_input.get("message"), str):
                    handback_text = block_input["message"]
    if handback_text is not None:
        return handback_text, "handback"

    fallback_text: str | None = None
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        text_parts = [block["text"] for block in content
                      if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
        if text_parts:
            fallback_text = "".join(text_parts)
    if fallback_text is not None:
        return fallback_text, "assistant_text"

    raise RecordError(f"nothing to record in {transcript}: no SubagentHandback and no assistant text")


def write(text: str, out: Path, force: bool) -> int:
    if out.exists() and not force:
        raise RecordError(f"{out} already exists; pass --force to overwrite")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, out)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return len(text.encode("utf-8"))
