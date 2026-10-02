## What changed and why

## Checks run

- [ ] `uv run ruff check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] `uv run python scripts/check_agents.py` (when the catalog or seats changed)
- [ ] `uv run python scripts/check_examples.py` (when the planner or examples changed)
- [ ] `uv run python scripts/gen_notices.py --check` (when dependencies changed)
- [ ] `claude plugin validate --strict .`

## Notes for the reviewer

Linked issue, if any:
