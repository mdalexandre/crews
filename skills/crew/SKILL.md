---
name: crew
description: Plan a deterministic Claude crew for a task: task specific roles, seated on fixed model and effort files, with a budget from four typed answers. Use when a task is worth delegating to subagents, when deciding how many workers, which model and effort, and whether a blind check role is needed. Triggers on 'plan a crew', 'how many agents for this', 'fan this out', 'crew plan', 'split this across workers'.
---

# crew

crews turns a task into a crew plan. Code fixes the model, the effort and the worker count; you write the roles for this task. The server plans; you dispatch. Each role receives a typed XML brief (`<crew_task>`) and returns a short typed result.

## Calling crew_plan

Call the MCP tool `mcp__plugin_crews_crews__crew_plan` with:

- `roles`: the roles object below (`task` plus `roles`)
- `answers`: the four typed task answers (optional; see below)
- `judge`: pass `"off"` unless a TypeSafe key is configured; with no key the plugin runs the judge off and says so in one warning
- `run_dir`: optional directory for the plan files (defaults to a directory under the plugin data directory)

It returns an envelope. `output.waves` holds the Agent calls to issue. Status BLOCKED means the plan was refused; `errors` names everything that is missing and how to fix it. Fix the roles, never the rule. `mcp__plugin_crews_crews__crew_budget` returns the budget alone and `mcp__plugin_crews_crews__crew_catalog` lists the seats.

If the MCP server is down, the command line twin is `python3 ${CLAUDE_PLUGIN_ROOT}/crew.py plan --roles roles.json --answers answers.json --judge off --run-dir DIR`; let its own stdout show the calls.

## The four answers

`answers` = `{need: {choice, confidence, probabilities}, difficulty: {score 0..3}, divisible: {noul 0..1}, needs_verifier: {noul 0..1}}`.

`need` picks a catalog row, and the row fixes the worker, lead and check models and efforts. Label honestly, because the label sets the price:

- Execution defaults to rows that seat Sonnet workers: `repeatable_task`, `bounded_retrieval`, `everyday_implementation`, `long_running_agentic`.
- A check a tool could make alone is `mechanical_verification` (Haiku).
- Use `difficult_review`, `integration_architecture` or `specialist` (Opus or Fable workers) only when the piece itself needs hard reasoning, never because the overall task matters.

The cell formula:

- fan out when `divisible` >= 0.60; parallel cells = min(2 + round(difficulty), 4) when fanning out, else 1
- a check cell when `needs_verifier` >= 0.60
- budget = parallel cells + 1 lead cell when fanning out + 1 check cell when required
- you are the lead, so the dispatched budget is the budget minus 1 when fanning out
- difficulty >= 2.50 raises the worker effort one step (Haiku has no effort)
- more than 8 cells, or more than the dispatched budget, is refused unless `allow` covers it
- with no judge the budget is 2 cells on the `everyday_implementation` row with a check

## roles.json

`{task, roles: [{name, kind, mission, deliverable, scope, slices, criteria, acceptance, answers, ...}]}`

- `name`: kebab case, named after what it does to what in THIS task (`rls-policy-auditor`, not `verifier`). A role named only by a function any task could have (verifier, worker, researcher, reviewer, implementer, lead, tester, helper, agent, assistant) is refused, as is a role whose `specific` score is below 0.50.
- `kind`: `research`, `produce`, `integrate` or `check`
- `mission`: shares at least one word of four or more letters with the task or the role's scope
- `deliverable`: a plain file name for the record you write afterwards with `crew record`. The agent never writes it.
- `scope`: write paths, produce and integrate only; roles and slices never overlap
- `slices`: `[{brief, scope}]` to split work across workers
- `acceptance`: observable conditions that define done; required for produce, research and integrate roles
- `criteria`: check roles only, lines `C1:`, `C2:`; a check role holds no scope
- `answers`: optional per role `{need, difficulty, divisible, specific}`
- optional: `authority`, `inputs`, `execution`, `returns` (`artifact` or `report`), `finding_format`, `blocked_when`, `must_not`, `read_scope`, `professional_frame` (`{profession, standards, method, watch_for}`)

### One complete working example

roles:

```json
{
  "task": "add rate limiting to the public search endpoint",
  "roles": [
    {
      "name": "search-endpoint-rate-limiter",
      "kind": "produce",
      "mission": "add a token bucket rate limiter to the public search endpoint",
      "deliverable": "rate-limiter-record.md",
      "scope": ["src/api/search.py", "src/api/rate_limit.py"],
      "acceptance": [
        "unauthenticated callers are limited to 30 requests per minute per IP",
        "a limited caller gets HTTP 429 with a Retry-After header"
      ],
      "authority": ["src/api/search.py as the current handler"],
      "answers": {"need": "everyday_implementation", "difficulty": 1, "divisible": 0.2, "specific": 0.9}
    },
    {
      "name": "rate-limiter-behavior-checker",
      "kind": "check",
      "mission": "verify the search endpoint rate limiter behaves per the criteria",
      "deliverable": "rate-limiter-verdict.md",
      "criteria": [
        "C1: unauthenticated callers are limited to 30 requests per minute per IP",
        "C2: a limited caller gets HTTP 429 with a Retry-After header"
      ],
      "inputs": ["src/api/search.py", "src/api/rate_limit.py"],
      "answers": {"need": "mechanical_verification", "difficulty": 0, "divisible": 0, "specific": 0.9}
    }
  ]
}
```

answers:

```json
{
  "need": {"choice": "everyday_implementation", "confidence": 0.8, "probabilities": {"everyday_implementation": 0.8, "repeatable_task": 0.2}},
  "difficulty": {"score": 1.0},
  "divisible": {"noul": 0.3},
  "needs_verifier": {"noul": 0.9}
}
```

## Dispatching the plan

Issue every call of a wave in ONE message so the cells of a wave run at the same time, and wait for the wave before the next. Waves run research, produce, integrate, check. For each call copy `subagent_type`, `description` and `prompt` verbatim from the plan. Never dispatch a crew seat any other way: the seat guard admits only the exact calls a recent plan returned.

When a later wave reads an earlier role's result as an `inputs` path, close the gap yourself after that wave, once per role:

```
python3 ${CLAUDE_PLUGIN_ROOT}/crew.py record --transcript <agent output file> --out <record_path>
```

`record_path` is the value the plan carries for that role. It prints one JSON line and never the extracted text. Never hand a check role a record: records hold producer conclusions.

## Return modes

Every role ends with "Do not return command transcripts or narrate your steps." No role writes a report file.

- artifact (produce, integrate): `STATUS`, `ARTIFACTS`, `RESULT`, `VERIFY`, `DECISION`
- report (research, or produce with `returns: report`): `STATUS`, one `F:` line per finding with `EVIDENCE:`, `COVERAGE`, `DECISION`
- verdict (check): `VERDICT`, `PASS`, `FAIL <ID>: ... | EVIDENCE: ...`, `UNVERIFIED`, `DECISION`

## The blind check role

A check role is an independent verifier, never the producer re-grading itself. Its brief carries only its mission, the objective, its own criteria and the paths of the artifact under check. It never sees a producer's `RESULT`, `VERIFY` or reasoning; its scope is `WRITE: NONE`; it never starts another agent. Write criteria as observable conditions, not as "confirm this is correct". The planner refuses a check role whose authored text matches the leak regex (for example "I verified that" or "the fix is correct"), and the qa_leak_check hook warns if a check dispatch prompt carries such text.

## The hooks and how to turn each one off

The plugin registers these hooks. All fail open: a defect in a hook never blocks a session.

- seat_guard (PreToolUse on Agent calls): blocks generic agents (`general-purpose`, `claude`, no `subagent_type`) and any crew seat dispatch that no recent plan returned. Off: set the plugin option `seat_guard_mode` to `advise` (warn instead of block), set `CREWS_SEAT_GUARD=0` in the environment, or disable the plugin.
- session_start (SessionStart): prints one line saying crews plans subagent crews. Off: disable the plugin.
- inline_budget (UserPromptSubmit, PostToolUse, PreToolUse on Bash): does nothing by default. When the plugin option `inline_budget_enabled` is true, it counts Read, Edit, Write, MultiEdit, NotebookEdit, Bash, Grep and Glob calls per turn and denies a Bash call once the count reaches `inline_budget` (default 5), unless `crew_plan` was called or a crew seat was dispatched in that turn. Off: set `inline_budget_enabled` to false.
- qa_leak_check (PreToolUse on Agent calls): advisory only; warns on stderr when a check seat prompt looks like it carries the author's verdict. Off: disable the plugin.

An organization setting that allows managed hooks only disables plugin hooks; the planner still works without them.

## Boundaries

The live TypeSafe judge runs only when a TypeSafe API key is configured; without one, pass declared answers. Rollback: `python3 ${CLAUDE_PLUGIN_ROOT}/crew.py uninstall --agents-dir <agents dir>` removes only generated seat files.
