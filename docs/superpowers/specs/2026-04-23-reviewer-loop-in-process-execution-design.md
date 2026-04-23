# Reviewer Loop In-Process Execution Design

## Purpose

Refactor `reviewer-loop` so the controller no longer launches nested `codex exec` subprocesses for reviewer and fixer passes. Preserve the existing CLI surface and artifact layout while moving agent execution into the current Codex runtime. Reduce the default review/fix loop cap from `7` to `5`.

## Problem Statement

The current controller shells out to nested `codex exec` runs for both reviewers and fixer. On hosts where nested Codex sandbox startup fails, the loop escalates before reviewers or fixer can do useful work. The failure mode is environmental, but the architecture makes the skill depend on nested sandbox creation that is not required by the user-facing contract.

The refactor should remove that dependency without weakening the loop's review discipline:

- reviewers stay review-only in behavior
- fixer remains the only actor allowed to edit the target repo
- required tests still gate every remediation round
- runs still emit the same artifact tree under `.codex/reviewer-loop-runs/<timestamp>/`

## Goals

- Preserve the existing command-line interface, including `--repo`, `--scope`, `--test-command`, model overrides, and explicit caps
- Preserve the current scope model and baseline-freezing behavior
- Preserve artifact paths and JSON payload shapes for reviewer, fixer, merged findings, and test logs
- Preserve parallel reviewer fan-out
- Remove nested `codex exec` as the primary reviewer/fixer execution mechanism
- Make `5` the default `max-review-rounds`

## Non-Goals

- Changing the semantics of required test gating
- Changing the review personas or schema formats
- Introducing resume support for interrupted runs
- Redesigning the prompt content beyond what is necessary to support the new executor boundary
- Requiring `--dangerously-bypass-codex-sandbox` for normal operation

## User-Facing Contract

The following remain stable:

- the top-level skill usage
- the Python controller entrypoint
- the artifact tree and filenames
- the meaning of `auto`, `uncommitted`, `last-commit`, `last-n-commits`, and `base-diff`
- the requirement to provide at least one test command
- the rule that tests must pass before returning to review

The only intentional visible behavior change is the default review/fix round cap moving from `7` to `5`.

## Architecture

### Current Model

Today the controller owns loop state, diff generation, test gating, and artifact writing, but reviewer and fixer execution are delegated by spawning `codex exec` subprocesses with prompt text and JSON schemas.

### Proposed Model

Replace subprocess-based reviewer/fixer execution with an in-process executor abstraction:

- `LoopExecutor` is a controller-facing interface for running one reviewer or fixer task
- the controller builds prompts, chooses schemas, and writes artifacts exactly as it does today
- the executor submits work through the current Codex runtime instead of a nested CLI process
- reviewer tasks and fixer tasks both return structured payloads plus execution logs that the controller persists to existing artifact paths

This keeps orchestration in ordinary Python while removing the nested sandbox boundary that caused the bubblewrap startup failure.

## Components

### Controller

The controller continues to own:

- argument parsing
- repo validation
- scope resolution
- diff snapshot generation
- reviewer fan-out
- finding aggregation and dedupe
- remediation/test retry loops
- run record updates
- artifact persistence

The controller no longer knows how agent execution is transported. It depends on the executor interface.

### Executor Interface

Introduce a transport-neutral execution boundary with two operations:

- `run_reviewer(...) -> ExecutionResult`
- `run_fixer(...) -> ExecutionResult`

`ExecutionResult` contains:

- parsed JSON payload
- captured stdout-like log text
- captured stderr-like log text
- stable metadata needed for error reporting

The interface does not expose sandbox flags or CLI assembly. Those belong to transport implementations, not loop policy.

### In-Process Runtime Adapter

Implement the default executor as an adapter that runs work through the current Codex runtime in-process.

Responsibilities:

- submit prompt, schema, model override, and working directory
- label runs as reviewer or fixer
- preserve parallel reviewer execution semantics
- return structured payloads without requiring a nested shell process

The adapter may internally use session-native agent APIs, but that detail stays behind the executor boundary.

### Artifact Writer

Artifact writing stays in the controller so the external contract does not change. For each reviewer/fixer run, the controller still writes:

- `prompt.txt`
- `result.json`
- `stdout.log`
- `stderr.log`

When the in-process runtime does not provide meaningful stdout/stderr streams, the adapter should return synthetic but useful log text such as:

- execution mode
- runtime identifier if available
- blocking reason or structured error summary

This preserves operator visibility without pretending a subprocess existed.

## Data Flow

### Review Round

1. Controller freezes the scope baseline and writes the round diff snapshot.
2. Controller builds one reviewer prompt per persona.
3. Controller submits reviewer tasks through the executor in parallel.
4. Controller writes each reviewer prompt, result, and logs to the existing artifact paths.
5. Controller aggregates findings and computes the merged verdict.

### Fix Round

1. Controller writes merged findings.
2. Controller builds a fixer prompt using the merged findings and optional failing-test summary.
3. Controller submits the fixer task through the executor.
4. Controller writes fixer prompt, result, and logs to the existing artifact paths.
5. Controller reruns required tests locally.
6. Only if tests pass does the loop return to reviewers.

## Read-Only Reviewer Semantics

Reviewers are logically read-only because the controller treats them as review producers only:

- reviewer prompts instruct review behavior only
- reviewer outputs must validate against the review schema
- reviewer tasks are never followed by direct test reruns or artifact rewriting beyond their own result files
- only the fixer path is allowed to produce repository edits

The design does not depend on a nested OS-level sandbox to preserve that role separation. It relies on executor routing and controller policy.

## Error Handling

### Runtime/Transport Failure

If the executor cannot start or cannot return a valid schema-conforming payload:

- raise a controller `LoopError`
- write the best available logs to the relevant artifact directory
- mark the run as escalated in `run.json`
- report a targeted runtime error instead of a raw nested sandbox failure

### Invalid Payload

If the runtime returns malformed JSON or violates the declared schema:

- treat the task as failed
- persist raw logs and partial output when available
- escalate with a message identifying the reviewer/fixer phase that failed

### Fixer Outcome

The controller should not treat fixer self-reporting as a replacement for the required test gate. Fixer status is advisory for escalation and diagnostics; the loop still relies on the required tests to determine whether the workspace is acceptable to return to review.

## Concurrency

Parallel reviewer fan-out remains a design requirement. The executor implementation must support one task per reviewer persona running concurrently without sharing mutable artifact paths.

The fixer remains single-threaded and serialized after reviewer aggregation.

## Configuration

- Keep `--max-review-rounds` as a CLI override
- Change its default value from `7` to `5`
- Keep `--max-test-fix-attempts` behavior unchanged
- Keep model override flags
- Retain the explicit bypass flag only as a compatibility escape hatch for any remaining subprocess-based fallback paths, not as the primary operating mode

## Migration Plan

1. Introduce executor/result abstractions alongside the existing subprocess helper.
2. Move controller reviewer/fixer code to depend on the executor interface.
3. Implement the in-process executor and make it the default path.
4. Keep the subprocess runner only as a narrow compatibility fallback while tests are updated.
5. Remove or de-emphasize subprocess-specific tests that assert exact `codex exec` command assembly.

## Testing Strategy

Add or update tests to cover:

- default `max-review-rounds == 5`
- controller reviewer fan-out uses the executor abstraction rather than directly calling `codex exec`
- reviewer and fixer prompts still land in the same artifact paths
- merged findings and test gating behavior remain unchanged
- fixer/test loop still requires passing tests before another review round
- runtime failures are surfaced as targeted controller errors

Keep existing tests for:

- scope resolution
- repo guidance extraction
- finding dedupe
- inline prompt embedding and budgets

## Open Questions Resolved

- Preserve CLI and artifact layout: yes
- Preserve parallel reviewers: yes
- Preserve required test gating: yes
- Change default review/fix cap to `5`: yes

## Success Criteria

The refactor is complete when:

- `reviewer-loop` no longer depends on nested `codex exec` for normal reviewer/fixer execution
- the same review loop can run in the current Codex runtime without the bubblewrap failure mode
- the CLI contract and artifact layout remain stable
- default review/fix rounds cap is `5`
- automated tests cover the executor boundary and the unchanged loop semantics
