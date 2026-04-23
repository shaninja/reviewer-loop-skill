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
    build_fixer_prompt,
    build_reviewer_prompt,
    build_run_record,
    create_run_dir,
    dedupe_findings,
    ensure_git_repo,
    load_repo_review_guidance,
    load_reviewer_roles,
    resolve_scope,
    run_codex_exec,
    run_required_tests,
    summarize_test_failures,
    validate_test_commands,
    verdict_from_findings,
    write_diff_snapshot,
    write_json,
)


def parse_args() -> argparse.Namespace:
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
    parser.add_argument("--max-review-rounds", type=int, default=7)
    parser.add_argument("--max-test-fix-attempts", type=int, default=3)
    parser.add_argument("--reviewer-model", default=None)
    parser.add_argument("--fixer-model", default=None)
    parser.add_argument("--dangerously-bypass-codex-sandbox", action="store_true")
    return parser.parse_args()


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def run_parallel_reviewers(
    repo: Path,
    roles: list[dict[str, object]],
    scope,
    diff_path: Path,
    repo_guidance_path: Path | None,
    reviewer_model: str | None,
    review_round_dir: Path,
    bypass_codex_sandbox: bool,
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
        completed, payload = run_codex_exec(
            repo,
            prompt,
            output_schema=REVIEW_OUTPUT_SCHEMA_PATH,
            output_file=output_path,
            sandbox_mode="read-only",
            model=reviewer_model,
            bypass_codex_sandbox=bypass_codex_sandbox,
        )
        write_text(stdout_path, completed.stdout)
        write_text(stderr_path, completed.stderr)
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
    *,
    phase: str,
    test_failure_summary: str | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_fixer_prompt(
        scope,
        findings_path,
        repo_guidance_path,
        test_commands,
        phase=phase,
        test_failure_summary=test_failure_summary,
    )
    prompt_path = output_dir / "prompt.txt"
    output_path = output_dir / "result.json"
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    write_text(prompt_path, prompt)
    completed, payload = run_codex_exec(
        repo,
        prompt,
        output_schema=FIX_OUTPUT_SCHEMA_PATH,
        output_file=output_path,
        sandbox_mode="workspace-write",
        model=fixer_model,
        bypass_codex_sandbox=bypass_codex_sandbox,
    )
    write_text(stdout_path, completed.stdout)
    write_text(stderr_path, completed.stderr)
    return payload


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
            run_fixer(
                repo,
                scope,
                bootstrap_findings_path,
                repo_guidance_path,
                args.test_commands,
                args.fixer_model,
                run_dir / "artifacts" / "bootstrap" / f"attempt-{bootstrap_attempt}",
                args.dangerously_bypass_codex_sandbox,
                phase="bootstrap-test-recovery",
                test_failure_summary=failure_summary,
            )
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

            if verdict in {"approved", "approved_with_notes"}:
                run_record["status"] = "completed"
                run_record["verdict"] = verdict
                run_record["findings"] = len(findings)
                write_json(run_dir / "run.json", run_record)
                print(json.dumps({"status": "completed", "verdict": verdict, "run_dir": str(run_dir)}))
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
                run_fixer(
                    repo,
                    scope,
                    merged_findings_path,
                    repo_guidance_path,
                    args.test_commands,
                    args.fixer_model,
                    fix_dir,
                    args.dangerously_bypass_codex_sandbox,
                    phase="review-remediation" if test_fix_attempt == 1 else "test-recovery",
                    test_failure_summary=summarize_test_failures(test_results) if test_results else None,
                )

                test_dir = run_dir / "artifacts" / "tests" / f"round-{review_round}" / f"attempt-{test_fix_attempt}"
                test_results = run_required_tests(repo, args.test_commands, test_dir)
                write_json(test_dir / "results.json", test_results)
                if all_tests_passed(test_results):
                    break

        raise LoopError("Review cap reached before the loop converged.")
    except LoopError as error:
        if "run_dir" in locals():
            run_record["status"] = "escalated"
            run_record["error"] = str(error)
            write_json(run_dir / "run.json", run_record)
            print(json.dumps({"status": "escalated", "error": str(error), "run_dir": str(run_dir)}))
        else:
            print(json.dumps({"status": "escalated", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
