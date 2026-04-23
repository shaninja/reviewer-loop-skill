# Reviewer Loop V1 Design

## Purpose

Create a repo-local source repository for a globally installed Codex skill that runs a multi-reviewer, fix-and-retest loop against any target Git repo in the current working tree.

## Decisions

- Source repo is an ordinary Git repository; global installation uses a symlink to the skill folder
- Globally installed skill name is `reviewer-loop`
- The skill is a thin entrypoint; ordinary Python owns the loop
- The loop runs in the target repo's current working tree, never in a separate worktree
- Scope model supports explicit scopes plus `auto`
- `auto` means:
  - dirty tree -> review the working tree against `HEAD`
  - clean tree -> review the last commit
- Required tests are mandatory per run
- A fixer may not hand work back to reviewers until the required tests pass
- Review rounds use fresh agents every round
- Each review round runs multiple specialist reviewers in parallel
- Repo-specific rules are loaded at runtime from the target repo, starting with `AGENTS.md`
- Artifacts live inside the target repo under `.codex/reviewer-loop-runs/<timestamp>/`
- Nested Codex runs default to sandboxed execution, with an explicit CLI opt-in to bypass that sandbox in constrained environments

## Architecture

### Skill Layer

The skill provides:

- trigger text for Codex discovery
- operator instructions for choosing scope and test commands
- stable command examples that invoke the bundled controller script

The skill does not own retries, state, or escalation.

### Controller Layer

The Python controller provides:

- scope resolution and baseline freezing
- per-round diff snapshots
- parallel reviewer fan-out
- result aggregation and dedupe
- fixer invocation
- mandatory test gating
- bounded retries and escalation
- durable run artifacts

## Scope Policy

Supported scopes:

- `auto`
- `uncommitted`
- `last-commit`
- `last-n-commits`
- `base-diff`

Safety rule:

- committed scopes (`last-commit`, `last-n-commits`, `base-diff`) require a clean repo at start
- `auto` resolves to `uncommitted` for a dirty repo and `last-commit` for a clean repo

The controller freezes a baseline revision and re-computes the diff from that baseline to the current working tree each round.

## Review Personas

The controller ships with four generic standing personas:

1. correctness
2. maintainability
3. scope-and-regression
4. edge-cases

Each persona has explicit focus and constraints. Repo-specific principles from the target repo are appended at runtime when available.

## Loop Topology

1. Resolve scope and create the run directory.
2. Load target repo guidance from `AGENTS.md` when present.
3. Run the required test commands against the current state.
4. If tests fail, enter a fixer-only test-recovery loop.
5. Once tests pass, generate a diff snapshot for the frozen baseline.
6. Launch all reviewer personas in parallel.
7. Aggregate and dedupe findings.
8. If only notes remain, stop successfully.
9. Otherwise invoke the fixer with the aggregated findings.
10. Re-run required tests.
11. If tests fail, stay in fixer mode until they pass or the test-fix cap is hit.
12. Re-run the reviewer round with a fresh diff snapshot.

## Initial Contracts

Reviewer output:

- summary
- findings
- optional blocked reason

Fixer output:

- status
- summary
- notes
- optional blocking reason

The controller derives the global verdict from merged findings:

- `approved`
- `approved_with_notes`
- `changes_requested`
- `blocked`

## Artifacts

Each run stores:

- `run.json`
- `scope.json`
- `repo-guidance.md`
- `artifacts/diff/round-<n>.md`
- `artifacts/reviews/round-<n>/<persona>/`
- `artifacts/fixes/round-<n>/attempt-<m>/`
- `artifacts/tests/<phase>/`

## Initial Limits

- maximum 7 review rounds
- maximum 3 test-fix attempts between review rounds

## Non-Goals For V1

- automatic commit creation
- automatic global packaging or pip install
- generic natural-language scope parsing inside the controller
- resume after process interruption
- reviewer-model orchestration beyond Codex CLI
