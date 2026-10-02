# Contributing to crews

Thanks for your interest. Crews is a small project; focused changes with a test are the easiest to review.

## Set up

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/mdalexandre/crews
cd crews
uv sync
```

## Checks to run before a pull request

```
uv run ruff check .
uv run mypy
uv run pytest
uv run python scripts/check_agents.py
uv run python scripts/check_examples.py
uv run python scripts/gen_notices.py --check
claude plugin validate --strict .
```

* `check_agents.py` fails when `agents/` differs from what `crew install` generates from `crews/catalog.json`. Change the catalog, never an agent file by hand, and regenerate the seats.
* `check_examples.py` plans every file in `examples/`.
* `gen_notices.py --check` fails when `THIRD_PARTY_NOTICES.md` is stale. Run it without `--check` after a dependency change in `uv.lock`.
* Tests must not write to your real state directory. The test suite sets `CREWS_HOME` to a temporary directory; do the same for any probe you run by hand.

## Guidelines

* Keep the planner deterministic. Nothing in the default install may call a network service.
* Hooks must fail open: a defect in a hook never blocks a session.
* Add one test for each new behavior.
* Type annotate new function signatures.
* Do not write owner specific paths or names into code, tests or docs.
* In prose, use commas, colons, parentheses or periods rather than dashes.

## Commit style

Short imperative subject line (about 72 characters or fewer), for example `Refuse a duplicate role name`. Add a body when the reason is not obvious. One logical change per commit.

## Pull requests

Describe what changed and why, list the checks you ran, and link the issue if there is one. By contributing you agree that your contribution is licensed under the MIT License.
