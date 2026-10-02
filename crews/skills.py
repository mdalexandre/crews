"""C-SKL: per role skill routing (typesafe-integration-v1.0.md section 2.5).

Interface
    enumerate_skills(globs) -> list[dict]        {id, name, description, path}, sorted by id
    prefilter(role, skills, k) -> list[dict]     BM25 ranking, positive only, ties by id, capped at k
    request(task, roles, candidates) -> dict     one TypeSafe request body for a whole plan
    parse(response, roles, candidates) -> list   validated per role answers; raises judge.JudgeParseError
    decide(parsed_role, thresholds, calibrated=True) -> dict
                                                  {"attach": id | None, "reason": str, "acted": bool, ...}
    attach(role, skill, index) -> dict           a new role dict with the skill's SKILL.md path added

Only ``name`` and ``description`` are read from each SKILL.md's YAML frontmatter, with a small hand
written scalar reader (plain, single or double quoted, and folded ``>``/literal ``|`` block scalars).
No YAML dependency is added. Nothing in this module makes a network call or touches the egress ledger;
DATA_CATEGORY and INCLUDES_WORKER_OUTPUT are exposed so the CLI wiring (C-CLI) can pass them to
``egress.gated_call``.
"""

from __future__ import annotations

import glob
import math
import os
import re
from pathlib import Path
from typing import Any

from crews.judge import JudgeParseError

# TSI-SKL-02: Jev chooses only among code supplied candidates plus this code defined option.
NONE_OPTION = "none"

# docs.typesafe.ai/primitives/choice.md: a choice question's ``criteria`` is an object mapping each
# option id to a description string, not a bare list of ids; the API returns HTTP 422 on a list.
# Each candidate's own SKILL.md description is that string, truncated so one role with many
# candidates cannot blow the request past a sane size.
_CRITERIA_DESC_MAX = 240

# request()'s model field, kept identical to crews/judge.py's default so both requests target the
# same TypeSafe model without importing a value crews/judge.py never exposed as public API.
MODEL = "jev-latest"

# C-EGR packet fields the CLI wiring must pass for every skill routing gated_call.
DATA_CATEGORY = "role_definitions"
INCLUDES_WORKER_OUTPUT = False

DEFAULT_SKILL_ROOTS: tuple[Path, ...] = (
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "plugins" / "cache",
)
DEFAULT_GLOBS: tuple[str, ...] = tuple(str(root / "**" / "SKILL.md") for root in DEFAULT_SKILL_ROOTS)

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]?(.*)$")
_BLOCK_INDICATORS = frozenset({"|", ">", "|-", "|+", ">-", ">+"})
_WORD_RE = re.compile(r"[a-z]{4,}")

# TSI-SKL-06: prefilter ranking constants (BM25 over skill name plus description, standard library
# only). k1 and b are the usual Okapi BM25 defaults; NAME_WEIGHT repeats each name token so a skill
# whose *name* matches the role carries more weight than one that only matches in prose.
_BM25_K1 = 1.5
_BM25_B = 0.75
_NAME_WEIGHT = 3
_PHRASE_BONUS = 2.0
_QUOTE_RE = re.compile(r"'([^'\n]{3,80})'|\"([^\"\n]{3,80})\"")

# Common function words that pass the four-letter length filter but carry no routing signal; stripped
# before scoring so overlap on "with", "this", "that" and the like never inflates a candidate's score.
_STOPWORDS = frozenset({
    "that", "this", "with", "from", "have", "will", "shall", "should", "would", "could", "when",
    "what", "where", "which", "while", "into", "onto", "also", "then", "than", "been", "being",
    "their", "there", "these", "those", "about", "after", "before", "over", "under", "only", "such",
    "more", "most", "some", "each", "both", "other", "same", "very", "just", "here", "upon", "does",
    "your", "were", "they", "them", "must", "make", "made", "like", "used", "uses", "using",
    "role", "roles", "task", "work",
})

_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ational", "ate"), ("ization", "ize"), ("fulness", ""), ("ousness", ""), ("iveness", ""),
    ("ities", "ity"), ("ments", "ment"), ("tions", "tion"), ("sions", "sion"), ("edly", ""),
    ("ing", ""), ("ies", "y"), ("ied", "y"), ("ers", ""), ("tion", ""), ("sion", ""), ("ment", ""),
    ("ed", ""), ("er", ""), ("es", ""), ("s", ""),
)


class SkillRefused(ValueError):
    """A skill cannot be attached: its path is not in the enumerated index, or is not a file."""


def _scalar(value: str, body: list[str], i: int) -> tuple[str, int]:
    """Parse the value of one frontmatter key, starting at its own line ``body[i]``. Returns the
    parsed text and the index of the first line after this key's value (its own line plus any
    continuation lines the key's style consumes)."""
    value = value.strip()
    n = len(body)
    if value and value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        return (value[1:end] if end != -1 else value[1:]), i + 1
    if value in _BLOCK_INDICATORS or value == "":
        parts: list[str] = []
        j = i + 1
        while j < n and (body[j].strip() == "" or body[j].startswith((" ", "\t"))):
            if body[j].strip():
                parts.append(body[j].strip())
            j += 1
        return " ".join(parts), j
    # Plain scalar, possibly continued on following unindented lines that do not start a new key.
    parts = [value]
    j = i + 1
    while j < n:
        nxt = body[j]
        if nxt.strip() == "" or nxt.strip() == "---":
            break
        if not nxt.startswith((" ", "\t")) and _KEY_RE.match(nxt):
            break
        parts.append(nxt.strip())
        j += 1
    return " ".join(parts), j


def _read_frontmatter(path: Path) -> tuple[str, str] | None:
    """Read only the ``name`` and ``description`` keys of the file's YAML frontmatter. None when
    either key is missing, empty, or the file has no ``---`` delimited frontmatter block."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    body = lines[1:end]
    found: dict[str, str] = {}
    i, n = 0, len(body)
    while i < n:
        line = body[i]
        if not line.strip() or line.startswith((" ", "\t")):
            i += 1
            continue
        m = _KEY_RE.match(line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2)
        val, nxt = _scalar(rest, body, i)
        if key in ("name", "description") and key not in found:
            found[key] = val.strip()
        i = nxt
    name, description = found.get("name", ""), found.get("description", "")
    if not name or not description:
        return None
    return name, description


def enumerate_skills(globs: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    """Enumerate installed skills from the given glob patterns. Deduplicated by resolved path,
    sorted by id. A name collision gives the (path-order) later file an id suffixed with its
    parent directory name; a further collision on that suffix is disambiguated with a counter so
    every id stays unique. Files with no ``name`` or no ``description`` are skipped."""
    found: dict[Path, None] = {}
    for pattern in globs:
        for match in glob.glob(os.path.expanduser(pattern), recursive=True):
            p = Path(match).resolve()
            if p.is_file():
                found.setdefault(p, None)
    seen_ids: set[str] = set()
    out: list[dict[str, str]] = []
    for path in sorted(found):
        meta = _read_frontmatter(path)
        if meta is None:
            continue
        name, description = meta
        skill_id = name
        if skill_id in seen_ids:
            skill_id = f"{name}-{path.parent.name}"
        counter = 2
        while skill_id in seen_ids:
            skill_id = f"{name}-{path.parent.name}-{counter}"
            counter += 1
        seen_ids.add(skill_id)
        out.append({"id": skill_id, "name": name, "description": description, "path": str(path)})
    out.sort(key=lambda s: s["id"])
    return out


def routable(index: list[dict[str, str]], exclude: list[str]) -> list[dict[str, str]]:
    """The enumerated ``index`` with every skill whose ``name`` is in ``exclude`` dropped, order
    preserved. A name (not an id) match, so every entry sharing an excluded name is dropped even
    when ``enumerate_skills`` gave duplicates of that name distinct, tool suffixed ids. Session
    level skills such as ``memory-first`` and ``using-superpowers`` (already run for every session
    by hooks, per ``typesafe.skill_routing.exclude_skills`` in ``crews/catalog.json``) must never
    reach a candidate list or a wide ranking question, so this runs before both."""
    excluded_names = set(exclude)
    return [s for s in index if s.get("name") not in excluded_names]


def _stem(word: str) -> str:
    """Light, deterministic suffix stripping (no dictionary, no external stemmer). Both the role
    text and every skill's tokens pass through this same function, so what matters is that related
    forms (``format``/``formatting``/``formatted``) collapse to the same stem, not that the stem is
    itself a real word."""
    for suf, repl in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)] + repl
    return word


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens of four or more letters, stopwords dropped, each reduced to its stem.
    A list (not a set): term frequency, inside both the query and each document, is part of the
    BM25 score below."""
    return [_stem(w) for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


def _role_text(role: dict[str, Any]) -> str:
    return " ".join([
        str(role.get("name") or ""),
        str(role.get("mission") or ""),
        " ".join(str(s).replace("/", " ").replace("_", " ") for s in (role.get("scope") or [])),
        " ".join(str(e) for e in (role.get("execution") or [])),
    ])


def _skill_doc_tokens(skill: dict[str, str]) -> list[str]:
    """A skill's scored vocabulary: its name's tokens repeated ``_NAME_WEIGHT`` times (a name match
    is a stronger routing signal than a description match) plus its description's tokens once."""
    name_tokens = _tokens(str(skill.get("name") or ""))
    desc_tokens = _tokens(str(skill.get("description") or ""))
    return name_tokens * _NAME_WEIGHT + desc_tokens


def _quoted_phrases(description: str) -> list[str]:
    """Trigger phrases a skill author quoted in its description, e.g. a phrase the skill says to
    fire on. Returned lowercase; empty when the description quotes nothing usable."""
    out = []
    for m in _QUOTE_RE.finditer(description):
        phrase = (m.group(1) or m.group(2) or "").strip().lower()
        if phrase:
            out.append(phrase)
    return out


def prefilter(role: dict[str, Any], skills: list[dict[str, str]], k: int) -> list[dict[str, str]]:
    """Okapi BM25 (k1=1.5, b=0.75, standard library only) over each skill's name (weighted) and
    description against the role's name, mission, scope, and execution text, both sides reduced to
    stemmed, stopword-filtered tokens. A skill whose description quotes a trigger phrase that
    appears verbatim in the role text gets a fixed bonus on top of the lexical score, so a multi
    word cue is not diluted into its individual words. Deterministic: BM25's inputs (term counts,
    document frequencies, lengths) are exact counts over the given corpus, not estimates. Only
    positive scores are kept; ties break by id; the result holds at most ``k`` candidates."""
    query_terms: dict[str, int] = {}
    for term in _tokens(_role_text(role)):
        query_terms[term] = query_terms.get(term, 0) + 1
    if not query_terms or not skills:
        return []
    role_text_lower = _role_text(role).lower()

    doc_tokens = [_skill_doc_tokens(skill) for skill in skills]
    doc_len = [len(toks) for toks in doc_tokens]
    n_docs = len(skills)
    avg_len = (sum(doc_len) / n_docs) if n_docs else 0.0

    doc_tf: list[dict[str, int]] = []
    doc_freq: dict[str, int] = {}
    for toks in doc_tokens:
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        doc_tf.append(tf)
        for t in tf:
            doc_freq[t] = doc_freq.get(t, 0) + 1

    scored: list[tuple[float, str, dict[str, str]]] = []
    for skill, tf, dl in zip(skills, doc_tf, doc_len):
        score = 0.0
        for term, qf in query_terms.items():
            f = tf.get(term)
            if not f:
                continue
            n_t = doc_freq.get(term, 0)
            idf = math.log(1.0 + (n_docs - n_t + 0.5) / (n_t + 0.5))
            norm = 1 - _BM25_B + _BM25_B * (dl / avg_len if avg_len else 1.0)
            score += qf * idf * (f * (_BM25_K1 + 1)) / (f + _BM25_K1 * norm)
        for phrase in _quoted_phrases(str(skill.get("description") or "")):
            if phrase in role_text_lower:
                score += _PHRASE_BONUS
        if score > 0:
            scored.append((-score, skill["id"], skill))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [skill for _, _, skill in scored[:k]]


def _criteria_description(skill: dict[str, str]) -> str:
    """A non-empty description string for one candidate's choice option, per TSI-SKL-06. Falls back
    to the skill's id when a candidate somehow carries no description (enumerate_skills already
    excludes those, but request() must not depend on a caller having gone through it)."""
    desc = str(skill.get("description") or "").strip()
    if not desc:
        desc = f"the {skill['id']} skill"
    return desc[:_CRITERIA_DESC_MAX]


def request(task: str, roles: list[dict[str, Any]], candidates: list[list[dict[str, str]]]) -> dict[str, Any]:
    """One TypeSafe request body for a whole plan. Per role i: ``skill.<i>`` is a Choice over that
    role's candidate ids plus the code defined ``none``; ``fits.<i>.<j>`` is a Noul per candidate j;
    ``procedure.<i>`` and ``generalist.<i>`` are Nouls independent of candidate count."""
    if len(roles) != len(candidates):
        raise ValueError("roles and candidates must have the same length")
    questions: dict[str, Any] = {}
    state_roles: list[dict[str, Any]] = []
    for i, (role, cands) in enumerate(zip(roles, candidates)):
        ref = f"`roles[{i}]`"
        ids = [c["id"] for c in cands]
        criteria: dict[str, str] = {cid: _criteria_description(c) for cid, c in zip(ids, cands)}
        criteria[NONE_OPTION] = "No listed skill genuinely fits this role's work."
        questions[f"skill.{i}"] = {
            "type": "choice",
            "criteria": criteria,
            "instructions": (
                f"Which installed skill, if any, should role {ref} read and apply before starting its "
                f"work? Choose {NONE_OPTION!r} when no candidate genuinely fits."
            ),
        }
        for j, cid in enumerate(ids):
            questions[f"fits.{i}.{j}"] = {
                "type": "noul",
                "instructions": f"Does the skill `{cid}` fit the work role {ref} must do?",
            }
        questions[f"procedure.{i}"] = {
            "type": "noul",
            "instructions": f"Would a careful expert follow a documented procedure for role {ref}'s work?",
        }
        questions[f"generalist.{i}"] = {
            "type": "noul",
            "instructions": (
                f"Could a generalist do role {ref}'s work well from general knowledge alone, with no "
                "specific documented procedure?"
            ),
        }
        state_roles.append({
            "name": role.get("name"),
            "mission": role.get("mission"),
            "candidates": [{"id": c["id"], "name": c["name"], "description": c["description"]} for c in cands],
        })
    return {"model": MODEL, "state": {"task": task, "roles": state_roles}, "questions": questions}


def _answers(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
        raise JudgeParseError("answers: missing")
    out: dict[str, Any] = response["answers"]
    return out


def _prob(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise JudgeParseError(f"{field}: must be a probability from 0 to 1")
    return float(value)


def _noul(ans: dict[str, Any], qid: str) -> float:
    a = ans.get(qid)
    if not isinstance(a, dict):
        raise JudgeParseError(f"{qid}: missing answer")
    return _prob(a.get("noul"), f"{qid}.noul")


def parse(response: Any, roles: list[dict[str, Any]], candidates: list[list[dict[str, str]]]
          ) -> list[dict[str, Any]]:
    """Validate every answer against its question. Raises judge.JudgeParseError naming the field."""
    if len(roles) != len(candidates):
        raise ValueError("roles and candidates must have the same length")
    ans = _answers(response)
    out: list[dict[str, Any]] = []
    for i, cands in enumerate(candidates):
        ids = [c["id"] for c in cands]
        options = set(ids) | {NONE_OPTION}
        qid = f"skill.{i}"
        a = ans.get(qid)
        if not isinstance(a, dict):
            raise JudgeParseError(f"{qid}: missing answer")
        choice = a.get("choice")
        if choice not in options:
            raise JudgeParseError(f"{qid}.choice: unknown option {choice!r}")
        fits: dict[str, float] = {cid: _noul(ans, f"fits.{i}.{j}") for j, cid in enumerate(ids)}
        procedure = _noul(ans, f"procedure.{i}")
        generalist = _noul(ans, f"generalist.{i}")
        out.append({"choice": choice, "fits": fits, "procedure": procedure, "generalist": generalist})
    return out


def decide(parsed_role: dict[str, Any], thresholds: dict[str, float], *, calibrated: bool = True
          ) -> dict[str, Any]:
    """Attach only when the choice is not ``none``, the fit Noul of the chosen candidate is at
    least thresholds['fit'], and the mean of procedure and (1 - generalist) is at least
    thresholds['gate'].

    ``calibrated`` defaults to True so every existing caller keeps today's behavior byte for byte.
    JEV-17 (mission SRD): an uncalibrated measurement must never produce an acting decision. Pass
    ``calibrated=False`` (the catalog's ``typesafe.skill_routing.calibrated`` flag, threaded through
    by ``crews.cli._apply_skill_routing``) to record the gate's own verdict for later calibration
    without ever letting it attach a skill: ``attach`` is always None and ``acted`` is always False,
    while ``observed_decision`` carries exactly what would have been decided had the threshold been
    trusted, so an operator reading the result can tell it only observed."""
    choice = parsed_role["choice"]
    if choice == NONE_OPTION:
        observed = {"attach": None, "reason": "choice: none"}
    else:
        fit = parsed_role["fits"].get(choice)
        if fit is None:
            observed = {"attach": None, "reason": f"choice {choice!r}: no fit Noul recorded for it"}
        elif fit < thresholds["fit"]:
            observed = {"attach": None, "reason": f"fit {fit} below threshold {thresholds['fit']}"}
        else:
            gate = (parsed_role["procedure"] + (1 - parsed_role["generalist"])) / 2
            if gate < thresholds["gate"]:
                observed = {"attach": None, "reason": f"gate {gate} below threshold {thresholds['gate']}"}
            else:
                observed = {"attach": choice, "reason": (f"fit {fit} at or above {thresholds['fit']}; "
                                                          f"gate {gate} at or above {thresholds['gate']}")}
    if not calibrated:
        return {"attach": None, "acted": False,
                "reason": ("uncalibrated skill_routing thresholds (JEV-17): TypeSafe was called and the "
                           "verdict below was recorded for calibration, but it was never applied"),
                "observed_decision": observed}
    return {**observed, "acted": True}


# docs.typesafe.ai/cookbooks/skill_suggestion.md wide ranking stage: one Choice question per chunk of
# the whole installed skill index, so BM25's low candidate recall (measured: the true skill inside the
# BM25 top 10 in 13 of 30 holdout cases) gets a second, independent ranking signal to merge with.
# Measured 2026-09-23 on the live TypeSafe API: a Choice question with more than 255 options returns
# HTTP 400 "Too many choices" (TSI authority in this task's brief), so each wide question is capped at
# that many options and the index is chunked across as many questions as it takes to cover it.
_WIDE_MAX_OPTIONS = 255
_WIDE_DESC_MAX_DEFAULT = 160


def _wide_chunks(index: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Split ``index`` into chunks of at most ``_WIDE_MAX_OPTIONS`` skills each, in index order, so
    every indexed skill lands in exactly one chunk. An empty index yields no chunks."""
    return [index[i:i + _WIDE_MAX_OPTIONS] for i in range(0, len(index), _WIDE_MAX_OPTIONS)]


def wide_request(task: str, roles: list[dict[str, Any]], index: list[dict[str, str]],
                 desc_chars: int = _WIDE_DESC_MAX_DEFAULT) -> dict[str, Any]:
    """One TypeSafe request body for the cookbook's wide ranking stage: per role ``i`` and chunk
    ``n`` of ``index`` (each chunk at most 255 skills, together covering every indexed skill exactly
    once), a Choice question ``wide.<i>.<n>`` whose ``criteria`` names every skill id in that chunk
    with its description truncated to ``desc_chars``. State carries only the task and, per role, its
    name and mission (the same shape ``request()`` above puts in ``state.roles``, without a
    candidate list, since the wide stage has not chosen candidates yet)."""
    chunks = _wide_chunks(index)
    questions: dict[str, Any] = {}
    for i, role in enumerate(roles):
        ref = f"`roles[{i}]`"
        for n, chunk in enumerate(chunks):
            criteria = {s["id"]: _criteria_description(s)[:desc_chars] for s in chunk}
            questions[f"wide.{i}.{n}"] = {
                "type": "choice",
                "criteria": criteria,
                "instructions": (
                    f"Which installed skill, if any among these options, would best help role {ref} do "
                    "its work? Answer even when no option fits especially well; this is one part of a "
                    "wider ranking, not a final choice."
                ),
            }
    state_roles = [{"name": role.get("name"), "mission": role.get("mission")} for role in roles]
    return {"model": MODEL, "state": {"task": task, "roles": state_roles}, "questions": questions}


def wide_parse(response: Any, roles: list[dict[str, Any]], index: list[dict[str, str]]
              ) -> list[list[str]]:
    """Per role, a ranked list of skill ids built by interleaving each chunk's own probability
    ranking (the same interleave ``wide_rank_probe.py`` measured): round 0 takes every chunk's top
    pick in chunk order, round 1 every chunk's second pick, and so on, until every chunk is
    exhausted. Raises ``judge.JudgeParseError`` naming the field on a malformed answer."""
    chunks = _wide_chunks(index)
    valid_ids = {s["id"] for s in index}
    ans = _answers(response)
    out: list[list[str]] = []
    for i, _role in enumerate(roles):
        per_chunk: list[list[str]] = []
        for n in range(len(chunks)):
            qid = f"wide.{i}.{n}"
            a = ans.get(qid)
            if not isinstance(a, dict):
                raise JudgeParseError(f"{qid}: missing answer")
            probs = a.get("probabilities")
            if not isinstance(probs, dict):
                raise JudgeParseError(f"{qid}.probabilities: missing")
            for sid, p in probs.items():
                _prob(p, f"{qid}.probabilities[{sid!r}]")
            ranked_ids = sorted(probs, key=lambda sid: -probs[sid])
            per_chunk.append([sid for sid in ranked_ids if sid in valid_ids])
        ranked: list[str] = []
        seen: set[str] = set()
        for rank in range(max((len(c) for c in per_chunk), default=0)):
            for chunk_ranked in per_chunk:
                if rank < len(chunk_ranked):
                    sid = chunk_ranked[rank]
                    if sid not in seen:
                        seen.add(sid)
                        ranked.append(sid)
        out.append(ranked)
    return out


def merge_candidates(bm25: list[dict[str, str]], wide_ids: list[str], index: list[dict[str, str]],
                     wide_k: int) -> list[dict[str, str]]:
    """The BM25 candidates, in order, followed by up to ``wide_k`` skills from ``wide_ids`` (itself
    already ranked, e.g. by ``wide_parse``) that are not already among them, by index lookup.
    Duplicate ids never appear twice; an id in ``wide_ids`` with no matching entry in ``index`` is
    skipped rather than raising, since the wide stage's own ranking is untrusted candidate material,
    not a validated selection."""
    by_id = {s["id"]: s for s in index}
    seen = {c["id"] for c in bm25}
    merged = list(bm25)
    added = 0
    for sid in wide_ids:
        if added >= wide_k:
            break
        if sid in seen:
            continue
        skill = by_id.get(sid)
        if skill is None:
            continue
        merged.append(skill)
        seen.add(sid)
        added += 1
    return merged


def attach(role: dict[str, Any], skill: dict[str, str], index: list[dict[str, str]]) -> dict[str, Any]:
    """Return a new role whose ``inputs`` gains the skill's absolute SKILL.md path and whose
    ``execution`` starts with one line telling the cell to read and apply it. Refuses a skill whose
    path is not in the enumerated index, or that is no longer a file."""
    valid_paths = {s["path"] for s in index}
    path = skill.get("path")
    if path not in valid_paths:
        raise SkillRefused(f"skill path {path!r} is not in the enumerated index")
    if not Path(path).is_file():
        raise SkillRefused(f"skill path {path!r} is not a file")
    new_role = dict(role)
    new_role["inputs"] = [*(role.get("inputs") or []), path]
    new_role["execution"] = [f"Read {path} before starting and apply its procedure to this work.",
                              *(role.get("execution") or [])]
    return new_role
