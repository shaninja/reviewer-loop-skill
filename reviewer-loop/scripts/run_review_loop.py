#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path

from reviewer_loop_lib import (
    FIX_OUTPUT_SCHEMA_PATH,
    REVIEW_OUTPUT_SCHEMA_PATH,
    LoopError,
    all_tests_passed,
    build_fixer_file_context,
    build_fixer_prompt,
    build_manager_closeout,
    build_reviewer_prompt,
    build_run_record,
    create_run_dir,
    dedupe_findings,
    ensure_git_repo,
    load_json_schema,
    load_repo_review_guidance,
    load_reviewer_roles,
    resolve_scope,
    run_app_server_turn,
    run_required_tests,
    summarize_test_failures,
    validate_test_commands,
    verdict_from_findings,
    write_diff_snapshot,
    write_json,
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the reviewer loop against a target repo.")
    parser.add_argument("--repo", required=True, help="Target Git repository path.")
    parser.add_argument(
        "--scope",
        default="auto",
        choices=["auto", "uncommitted", "last-commit", "last-n-commits", "base-diff"],
    )
    parser.add_argument("--commit-count", type=int, default=None)
    parser.add_argument("--base-ref", default=None)
    parser.add_argument("--test-command", action="append", default=[], dest="test_commands")
    parser.add_argument("--max-review-rounds", type=int, default=5)
    parser.add_argument("--max-test-fix-attempts", type=int, default=3)
    parser.add_argument("--codex-timeout-seconds", type=positive_int, default=None)
    parser.add_argument("--reviewer-model", default=None)
    parser.add_argument("--fixer-model", default=None)
    parser.add_argument("--dangerously-bypass-codex-sandbox", action="store_true")
    return parser.parse_args(argv)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def apply_fixer_edits(repo: Path, payload: dict[str, object]) -> None:
    repo_root = repo.resolve()
    for raw_edit in payload.get("edits", []):
        edit = dict(raw_edit)
        if edit.get("action") != "replace":
            raise LoopError(f"Unsupported fixer edit action: {edit.get('action')}")

        relative_path = str(edit["path"])
        target = (repo / relative_path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError as error:
            raise LoopError(f"Fixer edit path escapes the repository: {relative_path}") from error
        if not target.exists():
            raise LoopError(f"Fixer edit path does not exist: {relative_path}")

        current = target.read_text()
        expected_old_text = str(edit["expected_old_text"])
        if expected_old_text not in current:
            raise LoopError(f"{relative_path} did not contain the expected text for replacement.")
        updated = current.replace(expected_old_text, str(edit["new_text"]), 1)
        target.write_text(updated)


def run_parallel_reviewers(
    repo: Path,
    roles: list[dict[str, object]],
    scope,
    diff_path: Path,
    repo_guidance_path: Path | None,
    reviewer_model: str | None,
    review_round_dir: Path,
    bypass_codex_sandbox: bool,
    codex_timeout_seconds: int | None,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []

    def invoke(role: dict[str, object]) -> dict[str, object]:
        role_name = str(role["name"])
        role_dir = review_round_dir / role_name
        role_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = role_dir / "prompt.txt"
        output_path = role_dir / "result.json"
        stdout_path = role_dir / "stdout.log"
        stderr_path = role_dir / "stderr.log"

        prompt = build_reviewer_prompt(role, scope, diff_path, repo_guidance_path)
        write_text(prompt_path, prompt)
        artifacts = run_app_server_turn(
            repo,
            prompt,
            output_schema=load_json_schema(REVIEW_OUTPUT_SCHEMA_PATH),
            sandbox_mode="read-only",
            model=reviewer_model,
            timeout_seconds=codex_timeout_seconds,
        )
        payload = artifacts.payload
        write_json(output_path, payload)
        write_text(stdout_path, artifacts.stdout_log)
        write_text(stderr_path, artifacts.stderr_log)
        payload["reviewer"] = role_name
        return payload

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(roles)) as executor:
        futures = [executor.submit(invoke, role) for role in roles]
        for future in concurrent.futures.as_completed(futures):
            payloads.append(future.result())

    return payloads


def run_fixer(
    repo: Path,
    scope,
    findings_path: Path,
    repo_guidance_path: Path | None,
    test_commands: list[str],
    fixer_model: str | None,
    output_dir: Path,
    bypass_codex_sandbox: bool,
    codex_timeout_seconds: int | None,
    *,
    phase: str,
    test_failure_summary: str | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_context_path = build_fixer_file_context(repo, findings_path, output_dir / "file-context.md")
    prompt = build_fixer_prompt(
        scope,
        findings_path,
        repo_guidance_path,
        test_commands,
        file_context_path,
        phase=phase,
        test_failure_summary=test_failure_summary,
    )
    prompt_path = output_dir / "prompt.txt"
    output_path = output_dir / "result.json"
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    write_text(prompt_path, prompt)
    artifacts = run_app_server_turn(
        repo,
        prompt,
        output_schema=load_json_schema(FIX_OUTPUT_SCHEMA_PATH),
        sandbox_mode="read-only",
        model=fixer_model,
        timeout_seconds=codex_timeout_seconds,
    )
    payload = artifacts.payload
    write_json(output_path, payload)
    write_text(stdout_path, artifacts.stdout_log)
    write_text(stderr_path, artifacts.stderr_log)
    return payload


def ensure_fixer_succeeded(payload: dict[str, object]) -> None:
    status = payload.get("status")
    if status == "fixed":
        return
    reason = str(payload.get("blocking_reason") or payload.get("summary") or "Fixer did not complete successfully.")
    if status == "blocked":
        raise LoopError(reason)
    raise LoopError(f"Fixer failed: {reason}")


def write_manager_closeout(run_dir: Path, scope, verdict: str, round_records: list[dict[str, object]]) -> Path:
    closeout_path = run_dir / "manager-closeout.md"
    write_text(
        closeout_path,
        build_manager_closeout(
            scope,
            verdict=verdict,
            round_records=round_records,
        ),
    )
    return closeout_path


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()

    try:
        ensure_git_repo(repo)
        validate_test_commands(args.test_commands)
        scope = resolve_scope(
            repo,
            args.scope,
            commit_count=args.commit_count,
            base_ref=args.base_ref,
        )

        run_dir = create_run_dir(repo)
        repo_guidance = load_repo_review_guidance(repo)
        repo_guidance_path = run_dir / "repo-guidance.md" if repo_guidance else None
        if repo_guidance_path:
            write_text(repo_guidance_path, repo_guidance + "\n")

        write_json(
            run_dir / "scope.json",
            {
                "requested": scope.requested,
                "effective": scope.effective,
                "baseline_expr": scope.baseline_expr,
                "baseline_sha": scope.baseline_sha,
                "description": scope.description,
            },
        )
        run_record = build_run_record(
            repo,
            scope,
            args.test_commands,
            args.max_review_rounds,
            args.max_test_fix_attempts,
        )
        write_json(run_dir / "run.json", run_record)

        bootstrap_findings_path = run_dir / "artifacts" / "bootstrap" / "findings.json"
        write_json(
            bootstrap_findings_path,
            {
                "phase": "bootstrap",
                "findings": [],
                "reason": "Initial required test gate before first review round.",
            },
        )

        bootstrap_test_dir = run_dir / "artifacts" / "tests" / "bootstrap"
        bootstrap_results = run_required_tests(repo, args.test_commands, bootstrap_test_dir)
        write_json(bootstrap_test_dir / "results.json", bootstrap_results)

        bootstrap_attempt = 0
        while not all_tests_passed(bootstrap_results):
            bootstrap_attempt += 1
            if bootstrap_attempt > args.max_test_fix_attempts:
                raise LoopError("Initial required tests did not pass before the review loop started.")

            failure_summary = summarize_test_failures(bootstrap_results)
            fix_payload = run_fixer(
                repo,
                scope,
                bootstrap_findings_path,
                repo_guidance_path,
                args.test_commands,
                args.fixer_model,
                run_dir / "artifacts" / "bootstrap" / f"attempt-{bootstrap_attempt}",
                args.dangerously_bypass_codex_sandbox,
                args.codex_timeout_seconds,
                phase="bootstrap-test-recovery",
                test_failure_summary=failure_summary,
            )
            ensure_fixer_succeeded(fix_payload)
            apply_fixer_edits(repo, fix_payload)
            bootstrap_results = run_required_tests(
                repo,
                args.test_commands,
                run_dir / "artifacts" / "tests" / "bootstrap" / f"attempt-{bootstrap_attempt}",
            )
            write_json(
                run_dir / "artifacts" / "tests" / "bootstrap" / f"attempt-{bootstrap_attempt}" / "results.json",
                bootstrap_results,
            )

        roles = load_reviewer_roles()
        round_records: list[dict[str, object]] = []
        for review_round in range(1, args.max_review_rounds + 1):
            run_record["review_round"] = review_round
            write_json(run_dir / "run.json", run_record)

            diff_path = write_diff_snapshot(
                repo,
                scope,
                run_dir / "artifacts" / "diff" / f"round-{review_round}.md",
            )
            review_round_dir = run_dir / "artifacts" / "reviews" / f"round-{review_round}"
            review_payloads = run_parallel_reviewers(
                repo,
                roles,
                scope,
                diff_path,
                repo_guidance_path,
                args.reviewer_model,
                review_round_dir,
                args.dangerously_bypass_codex_sandbox,
                args.codex_timeout_seconds,
            )

            blocked_reasons = [
                f"{payload['reviewer']}: {payload['blocked_reason']}"
                for payload in review_payloads
                if payload.get("blocked_reason")
            ]
            findings = dedupe_findings(review_payloads)
            verdict = verdict_from_findings(findings, blocked_reasons)

            merged_payload = {
                "verdict": verdict,
                "blocked_reasons": blocked_reasons,
                "summaries": {
                    payload["reviewer"]: payload["summary"] for payload in review_payloads
                },
                "findings": [finding.__dict__ for finding in findings],
            }
            merged_findings_path = review_round_dir / "merged-findings.json"
            write_json(merged_findings_path, merged_payload)

            round_record: dict[str, object] = {
                "round": review_round,
                "verdict": verdict,
                "findings": merged_payload["findings"],
                "fixes": [],
                "test_results": [],
            }
            round_records.append(round_record)

            if verdict in {"approved", "approved_with_notes"}:
                closeout_path = write_manager_closeout(run_dir, scope, verdict, round_records)
                run_record["status"] = "completed"
                run_record["verdict"] = verdict
                run_record["findings"] = len(findings)
                run_record["manager_closeout"] = str(closeout_path)
                write_json(run_dir / "run.json", run_record)
                print(json.dumps({
                    "status": "completed",
                    "verdict": verdict,
                    "run_dir": str(run_dir),
                    "manager_closeout": str(closeout_path),
                }))
                return 0

            if verdict == "blocked":
                raise LoopError("One or more reviewer agents returned a blocked result.")

            test_fix_attempt = 0
            test_results = []
            while True:
                test_fix_attempt += 1
                if test_fix_attempt > args.max_test_fix_attempts:
                    raise LoopError("Fix attempts hit the required-test retry cap.")

                fix_dir = run_dir / "artifacts" / "fixes" / f"round-{review_round}" / f"attempt-{test_fix_attempt}"
                fix_payload = run_fixer(
                    repo,
                    scope,
                    merged_findings_path,
                    repo_guidance_path,
                    args.test_commands,
                    args.fixer_model,
                    fix_dir,
                    args.dangerously_bypass_codex_sandbox,
                    args.codex_timeout_seconds,
                    phase="review-remediation" if test_fix_attempt == 1 else "test-recovery",
                    test_failure_summary=summarize_test_failures(test_results) if test_results else None,
                )
                ensure_fixer_succeeded(fix_payload)
                apply_fixer_edits(repo, fix_payload)
                round_record["fixes"].append(fix_payload)

                test_dir = run_dir / "artifacts" / "tests" / f"round-{review_round}" / f"attempt-{test_fix_attempt}"
                test_results = run_required_tests(repo, args.test_commands, test_dir)
                round_record["test_results"].extend(test_results)
                write_json(test_dir / "results.json", test_results)
                if all_tests_passed(test_results):
                    break

        raise LoopError("Review cap reached before the loop converged.")
    except LoopError as error:
        if "run_dir" in locals():
            run_record["status"] = "escalated"
            run_record["error"] = str(error)
            if "scope" in locals() and "round_records" in locals():
                closeout_path = write_manager_closeout(run_dir, scope, "escalated", round_records)
                run_record["manager_closeout"] = str(closeout_path)
            write_json(run_dir / "run.json", run_record)
            payload = {"status": "escalated", "error": str(error), "run_dir": str(run_dir)}
            if "closeout_path" in locals():
                payload["manager_closeout"] = str(closeout_path)
            print(json.dumps(payload))
        else:
            print(json.dumps({"status": "escalated", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
