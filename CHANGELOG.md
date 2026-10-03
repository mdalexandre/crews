# Changelog

All notable changes are recorded here. The format follows Keep a Changelog and the project uses Semantic Versioning (0.y.z: the public interface may still change).

## [Unreleased] 0.2.0

### Added

* `crew_escalate` MCP tool and `crew escalate` verb (`crews/escalate.py`): re-seat one planned role one step up, or repair it in place. Effort rises first, then model; never below the original seat; Fable only for need `specialist`.
* Per run caps of 4 escalations and 2 repair rounds, counted in `<run dir>/escalations.json`; beyond a cap the result is BLOCKED and names the condition.
* The escalated call is registered in the plan index, so the seat guard admits exactly that call.

## [0.1.0]

First public release.

### Added

* Deterministic planner: task specific roles in, seated cells and Agent calls out, with the cell budget computed from four typed answers.
* Catalog of needs, kinds, models and efforts, and 26 pre-generated seat agents.
* MCP tools `crew_catalog`, `crew_check`, `crew_budget` and `crew_plan`.
* Seat guard hook that blocks generic subagents and any crew seat dispatch no plan returned (other named agents are not affected), advisory check leak hook, session start line, and an opt-in inline execution budget hook.
* `crew` skill describing the brief contract and how to dispatch a plan.
* Optional TypeSafe judge, skill routing and brief check, off unless a key is configured and each call approved.
* Command line (`crew.py`) with `plan`, `budget`, `record`, `list`, `install`, `check` and `uninstall`.
* Documentation, examples and third party notices.
* Hook launcher `hooks/run.sh`: every hook starts through `sh`, using `python3` (3.9 or newer) when present, else `uv`, else it exits 0 (fail open). The hooks need python3 3.9+ or uv on the machine.
* Uninstall instructions remove the orphaned plugin cache (`rm -rf ~/.claude/plugins/cache/crews`), because Claude Code keeps the seat files there after `claude plugin uninstall`.

[0.1.0]: https://github.com/mdalexandre/crews/releases/tag/v0.1.0
