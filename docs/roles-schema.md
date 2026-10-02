# roles.json and task answers

This page describes what `crew_plan` (and `crew.py plan`) accepts. It is derived from the validator in `crews/planner.py` (`spec_errors`) and `crews/budget.py` (`check_answers`). The validator collects every defect in one pass and refuses with the whole list, each message naming the role and the field.

Working examples that plan successfully are in [../examples](../examples). `scripts/check_examples.py` plans each one.

## The roles file

A JSON object with two keys.

| Field | Type | Required | Constraint |
|---|---|---|---|
| `task` | string | yes | Non empty. The one line statement of the whole task. |
| `roles` | array of role objects | yes | Non empty. |

### A role

| Field | Type | Required | Constraint enforced |
|---|---|---|---|
| `name` | string | yes | Kebab case (`^[a-z0-9]+(-[a-z0-9]+)*$`). Must say what the role does to what in this task. Refused when it is a generic name (`verifier`, `worker`, `researcher`, `reviewer`, `implementer`, `lead`, `tester`, `helper`, `agent`, `assistant`) or when every word of it is a generic name or a kind. Unique in the file. |
| `kind` | string | yes | One of `research`, `produce`, `integrate`, `check`. |
| `mission` | string | yes | Non empty. Must share at least one word of four or more letters with the task or with the role's scope paths. |
| `deliverable` | string | yes | A plain file name: no `/`, not starting with a dot. It names the record file written afterwards by `crew record`; the agent never writes it. |
| `answers` | object | yes | Per role answers, see below. |
| `scope` | array of strings | produce and integrate that return an artifact: yes, non empty | Write paths. Each a non empty string. A check role must hold no scope. Scopes of different roles must not overlap (a path and its parent directory overlap). |
| `slices` | array of objects | no | Each slice is `{brief: string, scope: [paths]}`. `brief` is required. For a produce or integrate role that returns an artifact each slice also needs a non empty `scope`; slice scopes must not overlap each other or another role. Slices split the work across workers. |
| `criteria` | array of strings | check roles: yes, non empty | The observable conditions the check evaluates. Rendered as `C1:`, `C2:`. |
| `acceptance` | array of strings | produce, research, integrate: yes, non empty | The observable conditions that define done. Rendered as `A1:`, `A2:`. |
| `authority` | array of strings | no | Sources the role works from. Each a non empty string. |
| `inputs` | array of strings | no | Paths the role reads. A later wave also receives earlier record paths, except a check role, which instead gets the write scopes of the producer roles as its artifact under check and never a record. |
| `execution` | array of strings | no | Extra steps rendered under `<execution>` (not for check roles, which get a fixed protocol). |
| `blocked_when` | array of strings | no | Extra conditions under which the role stops BLOCKED. |
| `must_not` | array of strings | no | Extra prohibitions appended to the scope block. |
| `read_scope` | array of strings | no | What the role may read. Defaults to whatever the objective needs. |
| `returns` | `"artifact"` or `"report"` | no | Produce and integrate default to `artifact`, research to `report`. A research role cannot return `artifact` (it holds no Write tool). A check role must not set it (it returns a verdict). |
| `finding_format` | string | no | The shape of an `F:` line for a report role. Non empty when present. |
| `professional_frame` | object | no | `{profession, standards, method, watch_for}`. Unknown keys are refused. `profession` is 3 to 200 characters; the other three are lists of non empty strings. A phrase about years of experience is refused. Optional, and changes the style of the brief only. |

For a produce or integrate role that returns an artifact, `scope` (or the scopes of its slices) is required. A research role, or a role with `returns: "report"`, holds no write scope.

All list fields reject a non string or empty string item, and a non list value.

### Per role answers

`roles[].answers` is a flat object:

| Field | Type | Constraint |
|---|---|---|
| `need` | string | One of the needs in the catalog: `repeatable_task`, `bounded_retrieval`, `everyday_implementation`, `long_running_agentic`, `mechanical_verification`, `difficult_review`, `integration_architecture`, `specialist`. Picks the catalog row that fixes the model and effort. |
| `difficulty` | number | 0 to 3. At 2.5 or more the effort is raised one step. |
| `divisible` | number | 0 to 1. With slices, 0.60 or more fans the role out to `min(2 + round(difficulty), 4, number of slices)` workers. |
| `specific` | number | Optional, 0 to 1. When given it must be at least 0.50. |

### Check role rules

* It holds no write scope and its seat is read only.
* Its authored text (task, mission, criteria, authority, inputs, execution, read scope, must not, blocked when, and the professional frame) is scanned by the brief leak regex. A match, for example "I verified that" or "the fix is correct", refuses the plan. Write criteria as conditions to observe, never as a conclusion to confirm.
* The plan must hold a check role when the budget requires one (`needs_verifier` at 0.60 or more), otherwise it is refused with `missing kind check`.

## The four task answers

The `answers` argument is a nested object. It is not the flat shape of `roles[].answers`.

```json
{
  "need": {"choice": "everyday_implementation", "confidence": 0.8,
           "probabilities": {"everyday_implementation": 0.8, "repeatable_task": 0.2}},
  "difficulty": {"score": 1.0},
  "divisible": {"noul": 0.3},
  "needs_verifier": {"noul": 0.9}
}
```

| Field | Type | Constraint |
|---|---|---|
| `need.choice` | string | A catalog need (list above). |
| `need.confidence` | number | 0 to 1. Below 0.60 the more capable of the top two needs by `probabilities` is used. |
| `need.probabilities` | object | Optional. Keys are catalog needs, values numbers from 0 to 1. |
| `difficulty.score` | number | 0 to 3. At 2.5 or more the worker effort is raised one step. |
| `divisible.noul` | number | 0 to 1. At 0.60 or more the crew fans out. |
| `needs_verifier.noul` | number | 0 to 1. At 0.60 or more a check cell is required. |

Answers are required unless the live judge is used or `outage` is true. `outage` plans an outage budget (2 cells on `everyday_implementation`, with a check).

## crew_plan arguments

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `roles` | object | required | The roles file above. |
| `answers` | object | none | The four task answers. |
| `outage` | boolean | false | Plan the outage budget instead of declared answers. |
| `judge` | `"off"` or `"live"` | `"live"` | Use `"off"` unless a TypeSafe key is configured. With no key it falls back to off with a warning. |
| `skills` | `"off"` or `"live"` | `"live"` | Optional TypeSafe skill routing, same fallback. |
| `brief_check` | `"off"` or `"live"` | `"live"` | Optional TypeSafe brief check, same fallback. |
| `run_dir` | string | under the state directory | Where the plan files are written. |
| `allow` | integer | none | Accept up to this many cells past the cap of 8 or past the dispatched budget. |
| `force` | object | none | `{roles: {name: {model, efforts, workers}}}` to override a seat. |
| `verbose` | boolean | false | Return the full plan inline instead of the compact view. |

The command line twin is `python3 crew.py plan --roles FILE --answers FILE --judge off --skills off --brief-check off --run-dir DIR`.
