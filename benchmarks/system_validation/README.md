# System validation benchmark

Each `cases/<scene>/<case-set>.json` contains semantically equivalent initial
prompt variants, one fixed semantic revision base, and six independent local
revisions. Revisions never consume a stochastic initial-authoring output.

Heterogeneous case sets include expected rejections. A rejected revision is a
success only when the compound turn returns `no_change_result`, the eligibility
reason matches the case's `reason_code`, the semantic plan is unchanged, and
the compiler was not entered. Each rejection stores a semantic `probe` used to
verify the deterministic backend reason without parsing prose heuristically.

Wave-2 constraint-dense case sets (`*-X`) additionally use two optional
`initial.expected` fields scored by `_score_initial`:

- `required_orderings`: `{before, after}` pairs of action ids that must appear
  in that order within the same robot's task sequence in the resulting plan.
- `required_dependencies`: `{action_id, after_action_id}` pairs that must
  appear as semantic `after` edges (used for cross-robot precedence stated in
  the initial request).

Both are vacuously satisfied when absent, so pre-existing case sets score
exactly as before.

Difficulty-pilot case sets (`42-C`, `42-D`) use three additional `expected`
forms, all scored by `_score_revision`:

- `changes` (array) instead of `change`: a compound revision succeeds only if
  every listed change is applied in the same turn.
- `required_absent_action_ids`: action ids that must no longer exist after the
  revision (e.g. derived open/close actions orphaned by removing or
  retargeting the only task that needed the facility).
- `change.type: "precedence"` on a conversation-channel turn: scored by
  checking that `after_action_id` appears on the target semantic action's
  `after` list (timeline-channel precedence still scores via the edit's
  step-level `after`).

The runner calls the production `stream_compound_turn` core. Conversation
revisions run Author with `current_actions` and `previous_plan`; timeline
revisions send the backend-native `PlanEditDelta[]` as `edits` with
`intent_hint: "edit"`. The base is cloned before every run.

```powershell
# Validate case data without model or compiler calls.
uv run python scripts/run_system_validation_benchmark.py --all-scenes --dry-run

# Run one case set in the formal LLM/plan-edit evaluation mode. This does not
# start or call the MuJoCo skill service.
uv run python scripts/run_system_validation_benchmark.py --case 42-A

# Run one timeline revision through the production compound/edit/replay code.
uv run python scripts/run_system_validation_benchmark.py --case 42-A `
  --kind revision --revision reassign_apple_1_to_robot1

# Optional physical integration check. This is outside the paper's
# Initial Authoring / Local Revision benchmark and requires the scene-matched
# skill service to be running.
uv run python scripts/run_system_validation_benchmark.py --case 42-A `
  --compile-mode service

# Select every A/B case belonging to one scene.
uv run python scripts/run_system_validation_benchmark.py `
  --scene layout042_sorting --repeats 3
```

Results are appended as one record per run under `results/*.jsonl`. Each record
retains the terminal artifact, streamed events, component checks, latency,
case hash, Git commit, and Responses API token usage so summaries can be
recomputed without another model call. The `usage` object accumulates every
model request made by one benchmark unit, including multi-turn tool calling;
it reports request count, input, cached-input, cache-write, output, reasoning,
and total tokens. Timeline-only records store `usage: null` because they do not
call a model. The final console summary also totals usage by benchmark kind and
over the complete run.

The default `--compile-mode llm-only` measures Author and plan-edit outputs
without MuJoCo/RRT compilation. Its latency is therefore authoring/editing
latency rather than physical planning latency. `--compile-mode semantic` is
retained as a backwards-compatible alias. Do not mix optional `service` runs
into the paper's LLM-only success or latency statistics.
