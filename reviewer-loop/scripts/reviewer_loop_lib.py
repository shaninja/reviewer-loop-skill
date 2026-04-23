from __future__ import annotations

import json
import re
import subprocess
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parent.parent
REVIEWER_ROLES_PATH = SKILL_ROOT / "references" / "reviewer_roles.json"
REVIEW_OUTPUT_SCHEMA_PATH = SKILL_ROOT / "references" / "review_output_schema.json"
FIX_OUTPUT_SCHEMA_PATH = SKILL_ROOT / "references" / "fix_output_schema.json"
SEVERITY_ORDER = {"blocker": 4, "high": 3, "medium": 2, "low": 1, "nit": 0}
INTERNAL_RUNS_PREFIX = ".codex/reviewer-loop-runs/"


class LoopError(RuntimeError):
    pass


@dataclass
class ScopeResolution:
    requested: str
    effective: str
    baseline_expr: str
    baseline_sha: str
    description: str
    requires_clean_start: bool


@dataclass
class ReviewFinding:
    severity: str
    category: str
    title: str
    detail: str
    file: str
    line: int | None
    must_fix: bool
    reviewers: list[str] = field(default_factory=list)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise LoopError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout.strip()


def ensure_git_repo(repo: Path) -> None:
    git(repo, "rev-parse", "--show-toplevel")


def repo_is_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain"))


def repo_commit_count(repo: Path) -> int:
    return int(git(repo, "rev-list", "--count", "HEAD"))


def empty_tree_sha(repo: Path) -> str:
    return git(repo, "hash-object", "-t", "tree", "/dev/null")


def validate_test_commands(commands: list[str]) -> None:
    if not commands:
        raise LoopError("At least one --test-command is required.")


def resolve_scope(
    repo: Path,
    scope: str,
    *,
    commit_count: int | None = None,
    base_ref: str | None = None,
) -> ScopeResolution:
    dirty = repo_is_dirty(repo)
    total_commits = repo_commit_count(repo)

    if scope == "auto":
        if dirty:
            baseline_expr = "HEAD"
            effective = "uncommitted"
            description = "working tree against HEAD"
            requires_clean_start = False
        else:
            baseline_expr = "EMPTY_TREE" if total_commits == 1 else "HEAD^"
            effective = "last-commit"
            description = (
                "initial commit against the empty tree"
                if total_commits == 1
                else "last commit against its parent"
            )
            requires_clean_start = False
    elif scope == "uncommitted":
        baseline_expr = "HEAD"
        effective = scope
        description = "working tree against HEAD"
        requires_clean_start = False
    elif scope == "last-commit":
        if dirty:
            raise LoopError("last-commit requires a clean repository at the start of the run.")
        baseline_expr = "EMPTY_TREE" if total_commits == 1 else "HEAD^"
        effective = scope
        description = (
            "initial commit against the empty tree"
            if total_commits == 1
            else "last commit against its parent"
        )
        requires_clean_start = True
    elif scope == "last-n-commits":
        if dirty:
            raise LoopError("last-n-commits requires a clean repository at the start of the run.")
        if commit_count is None or commit_count < 1:
            raise LoopError("--commit-count must be >= 1 for last-n-commits.")
        if commit_count > total_commits:
            raise LoopError("--commit-count cannot exceed the number of commits in the repository.")
        baseline_expr = "EMPTY_TREE" if commit_count == total_commits else f"HEAD~{commit_count}"
        effective = scope
        description = (
            f"entire repository history ({commit_count} commit(s)) against the empty tree"
            if commit_count == total_commits
            else f"last {commit_count} commit(s)"
        )
        requires_clean_start = True
    elif scope == "base-diff":
        if dirty:
            raise LoopError("base-diff requires a clean repository at the start of the run.")
        if not base_ref:
            raise LoopError("--base-ref is required for base-diff.")
        baseline_expr = git(repo, "merge-base", base_ref, "HEAD")
        effective = scope
        description = f"merge-base({base_ref}, HEAD) against HEAD"
        requires_clean_start = True
    else:
        raise LoopError(f"Unsupported scope: {scope}")

    baseline_sha = empty_tree_sha(repo) if baseline_expr == "EMPTY_TREE" else git(repo, "rev-parse", baseline_expr)
    return ScopeResolution(
        requested=scope,
        effective=effective,
        baseline_expr=baseline_expr,
        baseline_sha=baseline_sha,
        description=description,
        requires_clean_start=requires_clean_start,
    )


def load_reviewer_roles() -> list[dict[str, Any]]:
    return json.loads(REVIEWER_ROLES_PATH.read_text())


def extract_markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start: int | None = None
    collected: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index
            collected.append(line)
            continue
        if start is None:
            continue
        if line.startswith("## "):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def load_repo_review_guidance(repo: Path) -> str:
    agents_path = repo / "AGENTS.md"
    if not agents_path.exists():
        return ""
    section = extract_markdown_section(agents_path.read_text(), "## Code Review Workflow")
    return section.strip()


def create_run_dir(repo: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = repo / ".codex" / "reviewer-loop-runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json_payload(path: Path) -> Any:
    raw = path.read_text().strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    object_match = re.search(r"(\{.*\})", raw, re.DOTALL)
    if object_match:
        return json.loads(object_match.group(1))

    raise LoopError(f"Could not parse JSON payload from {path}")


def safe_read_text(path: Path, *, max_chars: int = 20000) -> str:
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return "<binary or non-text file omitted>"
    if len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated>..."
    return text


def is_internal_artifact_path(path: str) -> bool:
    normalized = path.strip()
    if normalized.startswith("?? "):
        normalized = normalized[3:]
    if "\t" in normalized:
        normalized = normalized.split("\t", 1)[-1]
    return normalized == ".codex/" or normalized.startswith(INTERNAL_RUNS_PREFIX)


def filter_output_lines(raw: str) -> str:
    kept = [line for line in raw.splitlines() if not is_internal_artifact_path(line)]
    return "\n".join(kept)


def write_diff_snapshot(repo: Path, scope: ScopeResolution, output_path: Path) -> Path:
    status_output = filter_output_lines(git(repo, "status", "--short"))
    tracked_diff = git(repo, "diff", "--binary", scope.baseline_sha, "--")
    name_status = filter_output_lines(git(repo, "diff", "--name-status", scope.baseline_sha, "--"))
    untracked = [
        path
        for path in git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
        if not is_internal_artifact_path(path)
    ]

    parts = [
        "# Reviewer Loop Diff Snapshot",
        "",
        f"- Requested scope: `{scope.requested}`",
        f"- Effective scope: `{scope.effective}`",
        f"- Baseline expression: `{scope.baseline_expr}`",
        f"- Baseline SHA: `{scope.baseline_sha}`",
        f"- Description: {scope.description}",
        "",
        "## Git Status",
        "",
        "```text",
        status_output or "<clean>",
        "```",
        "",
        "## Changed Paths",
        "",
        "```text",
        name_status or "<none>",
        "```",
        "",
        "## Tracked Diff",
        "",
        "```diff",
        tracked_diff or "",
        "```",
    ]

    if untracked:
        parts.extend(["", "## Untracked Files"])
        for relative_name in untracked:
            file_path = repo / relative_name
            parts.extend(
                [
                    "",
                    f"### {relative_name}",
                    "",
                    "```text",
                    safe_read_text(file_path),
                    "```",
                ]
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts) + "\n")
    return output_path


def build_reviewer_prompt(
    role: dict[str, Any],
    scope: ScopeResolution,
    diff_path: Path,
    repo_guidance_path: Path | None,
) -> str:
    guidance_block = (
        f"Repo-specific review guidance is in `{repo_guidance_path}`. Read it and follow it.\n"
        if repo_guidance_path
        else "No repo-specific review guidance was found.\n"
    )
    constraints = "\n".join(f"- {item}" for item in role["constraints"])
    return textwrap.dedent(
        f"""
        Review the current change set in this repository.

        Persona: {role["name"]}
        Focus: {role["focus"]}
        Constraints:
        {constraints}

        Scope:
        - Requested scope: {scope.requested}
        - Effective scope: {scope.effective}
        - Baseline: {scope.baseline_sha}
        - Description: {scope.description}

        The diff snapshot for this round is in `{diff_path}`.
        {guidance_block}
        Review only the requested change set. Do not ask for unrelated improvements.
        Prefer concrete, file-specific findings. Use severities `nit`, `low`, `medium`, `high`, or `blocker`.
        Set `must_fix` to true when the finding should block completion.

        Return JSON only. Do not wrap it in Markdown fences.
        """
    ).strip()


def build_fixer_prompt(
    scope: ScopeResolution,
    findings_path: Path,
    repo_guidance_path: Path | None,
    test_commands: list[str],
    *,
    phase: str,
    test_failure_summary: str | None = None,
) -> str:
    guidance_block = (
        f"Repo-specific review guidance is in `{repo_guidance_path}`. Read it and follow it.\n"
        if repo_guidance_path
        else "No repo-specific review guidance was found.\n"
    )
    tests_block = "\n".join(f"- {command}" for command in test_commands)
    failure_block = (
        f"\nThe last required test run failed.\nSummary:\n{test_failure_summary}\n"
        if test_failure_summary
        else ""
    )
    return textwrap.dedent(
        f"""
        Fix the current repository state in place.

        Scope:
        - Requested scope: {scope.requested}
        - Effective scope: {scope.effective}
        - Baseline: {scope.baseline_sha}
        - Description: {scope.description}

        Fix phase: {phase}
        Aggregated findings are in `{findings_path}`.
        {guidance_block}
        Required test commands that the controller will rerun after you finish:
        {tests_block}
        {failure_block}
        Do not commit, stash, reset, or create a new worktree.
        Keep changes tightly scoped to the findings and required test fixes.

        Return JSON only. Do not wrap it in Markdown fences.
        """
    ).strip()


def build_codex_exec_command(
    repo: Path,
    *,
    output_schema: Path,
    output_file: Path,
    sandbox_mode: str,
    model: str | None,
    bypass_codex_sandbox: bool,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "-C",
        str(repo),
        "--output-schema",
        str(output_schema),
        "-o",
        str(output_file),
    ]
    if bypass_codex_sandbox:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    elif sandbox_mode == "workspace-write":
        command.append("--full-auto")
    else:
        command.extend(["-s", sandbox_mode])
    if model:
        command.extend(["-m", model])
    return command


def run_codex_exec(
    repo: Path,
    prompt: str,
    *,
    output_schema: Path,
    output_file: Path,
    sandbox_mode: str,
    model: str | None = None,
    bypass_codex_sandbox: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Any]:
    command = build_codex_exec_command(
        repo,
        output_schema=output_schema,
        output_file=output_file,
        sandbox_mode=sandbox_mode,
        model=model,
        bypass_codex_sandbox=bypass_codex_sandbox,
    )

    result = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise LoopError(result.stderr.strip() or result.stdout.strip() or "codex exec failed")
    return result, read_json_payload(output_file)


def summarize_test_failures(results: list[dict[str, Any]]) -> str:
    failed = [item for item in results if item["returncode"] != 0]
    lines: list[str] = []
    for item in failed:
        lines.append(f"Command: {item['command']}")
        lines.append(f"Exit code: {item['returncode']}")
        tail = item["combined_output"].splitlines()[-20:]
        lines.append("Output tail:")
        lines.extend(tail or ["<no output>"])
        lines.append("")
    return "\n".join(lines).strip()


def run_required_tests(repo: Path, commands: list[str], artifact_dir: Path) -> list[dict[str, Any]]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, command in enumerate(commands, start=1):
        result = subprocess.run(
            command,
            cwd=repo,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
        )
        combined_output = result.stdout + ("\n" if result.stdout and result.stderr else "") + result.stderr
        log_path = artifact_dir / f"command-{index}.log"
        log_path.write_text(combined_output)
        results.append(
            {
                "command": command,
                "returncode": result.returncode,
                "log_path": str(log_path),
                "combined_output": combined_output,
            }
        )
    return results


def all_tests_passed(results: list[dict[str, Any]]) -> bool:
    return all(item["returncode"] == 0 for item in results)


def dedupe_findings(payloads: list[dict[str, Any]]) -> list[ReviewFinding]:
    merged: dict[tuple[str, int | None, str, str], ReviewFinding] = {}
    for payload in payloads:
        reviewer_name = payload["reviewer"]
        for raw_finding in payload["findings"]:
            key = (
                raw_finding["file"],
                raw_finding["line"],
                raw_finding["title"],
                raw_finding["category"],
            )
            if key not in merged:
                merged[key] = ReviewFinding(
                    severity=raw_finding["severity"],
                    category=raw_finding["category"],
                    title=raw_finding["title"],
                    detail=raw_finding["detail"],
                    file=raw_finding["file"],
                    line=raw_finding["line"],
                    must_fix=raw_finding["must_fix"],
                    reviewers=[reviewer_name],
                )
                continue

            existing = merged[key]
            if reviewer_name not in existing.reviewers:
                existing.reviewers.append(reviewer_name)
            if SEVERITY_ORDER[raw_finding["severity"]] > SEVERITY_ORDER[existing.severity]:
                existing.severity = raw_finding["severity"]
            if len(raw_finding["detail"]) > len(existing.detail):
                existing.detail = raw_finding["detail"]
            existing.must_fix = existing.must_fix or raw_finding["must_fix"]

    return sorted(
        merged.values(),
        key=lambda finding: (
            -SEVERITY_ORDER[finding.severity],
            finding.file,
            finding.line if finding.line is not None else -1,
            finding.title,
        ),
    )


def verdict_from_findings(findings: list[ReviewFinding], blocked_reasons: list[str]) -> str:
    if blocked_reasons:
        return "blocked"
    if not findings:
        return "approved"
    if any(finding.must_fix or SEVERITY_ORDER[finding.severity] >= SEVERITY_ORDER["medium"] for finding in findings):
        return "changes_requested"
    return "approved_with_notes"


def build_run_record(
    repo: Path,
    scope: ScopeResolution,
    test_commands: list[str],
    max_review_rounds: int,
    max_test_fix_attempts: int,
) -> dict[str, Any]:
    return {
        "repo": str(repo),
        "scope": asdict(scope),
        "test_commands": test_commands,
        "max_review_rounds": max_review_rounds,
        "max_test_fix_attempts": max_test_fix_attempts,
        "status": "running",
        "review_round": 0,
    }
