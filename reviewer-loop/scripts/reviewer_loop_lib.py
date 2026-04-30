from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
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
INLINE_DIFF_PROMPT_MAX_CHARS = 120000
INLINE_FINDINGS_PROMPT_MAX_CHARS = 80000
INLINE_GUIDANCE_PROMPT_MAX_CHARS = 30000
INLINE_FILE_CONTEXT_PROMPT_MAX_CHARS = 80000
APP_SERVER_CLIENT_NAME = "reviewer-loop"
APP_SERVER_CLIENT_VERSION = "0.1.0"
APP_SERVER_REASONING_EFFORT = "medium"


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


@dataclass
class ExecutionArtifacts:
    payload: dict[str, Any]
    stdout_log: str
    stderr_log: str


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


def parse_json_payload(raw: str) -> Any:
    raw = raw.strip()
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

    raise LoopError("Could not parse JSON payload.")


def read_json_payload(path: Path) -> Any:
    try:
        return parse_json_payload(path.read_text())
    except LoopError as error:
        raise LoopError(f"Could not parse JSON payload from {path}") from error


def safe_read_text(path: Path, *, max_chars: int | None = 20000) -> str:
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return "<binary or non-text file omitted>"
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated>..."
    return text


def read_embedded_context(path: Path, *, label: str, max_chars: int) -> str:
    content = safe_read_text(path, max_chars=None)
    if len(content) > max_chars:
        raise LoopError(
            f"{label} is {len(content)} characters, which exceeds the inline prompt budget of {max_chars}. "
            "Narrow the review scope or split the run into smaller slices."
        )
    return content


def render_embedded_block(label: str, content: str) -> str:
    start_marker = f"<<<REVIEWER_LOOP_BEGIN {label}>>>"
    end_marker = f"<<<REVIEWER_LOOP_END {label}>>>"
    escaped_content = content.replace(start_marker, f"\\{start_marker}").replace(end_marker, f"\\{end_marker}")
    return "\n".join(
        [
            start_marker,
            escaped_content,
            end_marker,
        ]
    )


def load_json_schema(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise LoopError(f"Schema at {path} must be a JSON object.")
    return payload


def build_thread_start_request(
    *,
    cwd: Path,
    sandbox: str,
    model: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(cwd),
        "approvalPolicy": "never",
        "sandbox": sandbox,
        "personality": "pragmatic",
        "developerInstructions": (
            "Use the prompt as the primary source of truth. "
            "Do not call MCP discovery tools unless the prompt explicitly requires them."
        ),
    }
    if model:
        params["model"] = model
    return {
        "method": "thread/start",
        "params": params,
    }


def build_turn_start_request(
    *,
    thread_id: str,
    prompt: str,
    output_schema: dict[str, Any],
    model: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        "outputSchema": output_schema,
        "approvalPolicy": "never",
    }
    if model:
        params["model"] = model
    return {
        "method": "turn/start",
        "params": params,
    }


def build_app_server_command() -> list[str]:
    return [
        "codex",
        "app-server",
        "-c",
        f'model_reasoning_effort="{APP_SERVER_REASONING_EFFORT}"',
        "--listen",
        "stdio://",
    ]


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
    diff_snapshot = read_embedded_context(
        diff_path,
        label="DIFF SNAPSHOT",
        max_chars=INLINE_DIFF_PROMPT_MAX_CHARS,
    )
    guidance_block = (
        "Repo-specific review guidance is included below. Follow it.\n\n"
        f"{render_embedded_block('REPO GUIDANCE', read_embedded_context(repo_guidance_path, label='REPO GUIDANCE', max_chars=INLINE_GUIDANCE_PROMPT_MAX_CHARS))}\n"
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

        All review context you need is embedded below. Do not rely on local file reads or shell commands unless they are absolutely necessary to understand a concrete finding.

        Diff snapshot:
        {render_embedded_block('DIFF SNAPSHOT', diff_snapshot)}

        {guidance_block}
        Review only the requested change set. Do not ask for unrelated improvements.
        Prefer concrete, file-specific findings. Use severities `nit`, `low`, `medium`, `high`, or `blocker`.
        Set `must_fix` to true when the finding should block completion.

        Return JSON only. Do not wrap it in Markdown fences.
        """
    ).strip()


def build_fixer_file_context(repo: Path, findings_path: Path, output_path: Path) -> Path | None:
    findings_payload = read_json_payload(findings_path)
    targets = sorted(
        {
            str(item["file"])
            for item in findings_payload.get("findings", [])
            if item.get("file")
        }
    )
    if not targets:
        return None

    parts = ["# Reviewer Loop Fixer File Context"]
    for relative_name in targets:
        file_path = repo / relative_name
        if not file_path.exists() or not file_path.is_file():
            continue
        parts.extend(
            [
                "",
                f"## {relative_name}",
                "",
                "```text",
                safe_read_text(file_path, max_chars=None),
                "```",
            ]
        )

    content = "\n".join(parts).strip()
    if content == "# Reviewer Loop Fixer File Context":
        return None
    if len(content) > INLINE_FILE_CONTEXT_PROMPT_MAX_CHARS:
        raise LoopError(
            f"FILE CONTEXT is {len(content)} characters, which exceeds the inline prompt budget of "
            f"{INLINE_FILE_CONTEXT_PROMPT_MAX_CHARS}. Narrow the review scope or fix findings in smaller slices."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content + "\n")
    return output_path


def build_fixer_prompt(
    scope: ScopeResolution,
    findings_path: Path,
    repo_guidance_path: Path | None,
    test_commands: list[str],
    file_context_path: Path | None = None,
    *,
    phase: str,
    test_failure_summary: str | None = None,
) -> str:
    findings_block = read_embedded_context(
        findings_path,
        label="AGGREGATED FINDINGS",
        max_chars=INLINE_FINDINGS_PROMPT_MAX_CHARS,
    )
    guidance_block = (
        "Repo-specific review guidance is included below. Follow it.\n\n"
        f"{render_embedded_block('REPO GUIDANCE', read_embedded_context(repo_guidance_path, label='REPO GUIDANCE', max_chars=INLINE_GUIDANCE_PROMPT_MAX_CHARS))}\n"
        if repo_guidance_path
        else "No repo-specific review guidance was found.\n"
    )
    file_context_block = (
        "Relevant current file contents are embedded below.\n\n"
        f"{render_embedded_block('FILE CONTEXT', read_embedded_context(file_context_path, label='FILE CONTEXT', max_chars=INLINE_FILE_CONTEXT_PROMPT_MAX_CHARS))}\n"
        if file_context_path
        else "No file context was embedded for this fixer turn.\n"
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
        Do not rely on local file reads or shell commands for review context. The aggregated findings and repo guidance are embedded below.

        Aggregated findings:
        {render_embedded_block('AGGREGATED FINDINGS', findings_block)}

        {guidance_block}
        {file_context_block}
        Required test commands that the controller will rerun after you finish:
        {tests_block}
        {failure_block}
        Do not commit, stash, reset, or create a new worktree.
        Keep changes tightly scoped to the findings and required test fixes.
        Do not modify files directly in this turn.
        Return exact replacement edits only in the `edits` array.
        Each edit must use:
        - a repo-relative `path`
        - action `replace`
        - exact current `expected_old_text`
        - exact replacement `new_text`
        If you cannot produce safe exact replacements, return `status: "blocked"` with a clear `blocking_reason`.

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
    timeout_seconds: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], Any]:
    command = build_codex_exec_command(
        repo,
        output_schema=output_schema,
        output_file=output_file,
        sandbox_mode=sandbox_mode,
        model=model,
        bypass_codex_sandbox=bypass_codex_sandbox,
    )

    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise LoopError(f"codex exec timed out after {timeout_seconds}s") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "codex exec failed"
        raise LoopError(message)

    return result, read_json_payload(output_file)


def _format_line_log(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _pipe_reader(
    stream,
    lines: list[str],
    *,
    message_queue: queue.Queue[str | None] | None = None,
) -> None:
    try:
        for line in stream:
            stripped = line.rstrip("\n")
            lines.append(stripped)
            if message_queue is not None:
                message_queue.put(stripped)
    finally:
        if message_queue is not None:
            message_queue.put(None)


def _is_server_request(message: dict[str, Any]) -> bool:
    return "id" in message and "method" in message and "result" not in message and "error" not in message


def _send_json_rpc_request(stdin, request_id: int, payload: dict[str, Any]) -> None:
    message = {"id": request_id, **payload}
    stdin.write(json.dumps(message) + "\n")
    stdin.flush()


def _send_json_rpc_error(stdin, request_id: Any, message: str) -> None:
    stdin.write(
        json.dumps(
            {
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": message,
                },
            }
        )
        + "\n"
    )
    stdin.flush()


def run_app_server_turn(
    repo: Path,
    prompt: str,
    *,
    output_schema: dict[str, Any],
    sandbox_mode: str,
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> ExecutionArtifacts:
    process = subprocess.Popen(
        build_app_server_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    stdout_queue: queue.Queue[str | None] = queue.Queue()

    stdout_thread = threading.Thread(
        target=_pipe_reader,
        args=(process.stdout, stdout_lines),
        kwargs={"message_queue": stdout_queue},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pipe_reader,
        args=(process.stderr, stderr_lines),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    def next_message() -> dict[str, Any]:
        if timeout_seconds is None:
            line = stdout_queue.get()
        else:
            remaining = timeout_seconds - (time.monotonic() - start_time)
            if remaining <= 0:
                raise LoopError(f"codex app-server turn timed out after {timeout_seconds}s")
            line = stdout_queue.get(timeout=remaining)
        if line is None:
            raise LoopError("codex app-server closed stdout before the turn completed")
        try:
            return json.loads(line)
        except json.JSONDecodeError as error:
            raise LoopError(f"codex app-server emitted invalid JSON: {line}") from error

    start_time = time.monotonic()

    try:
        _send_json_rpc_request(
            process.stdin,
            1,
            {
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": APP_SERVER_CLIENT_NAME,
                        "version": APP_SERVER_CLIENT_VERSION,
                    },
                    "capabilities": {},
                },
            },
        )

        initialized = False
        while not initialized:
            message = next_message()
            if _is_server_request(message):
                _send_json_rpc_error(
                    process.stdin,
                    message["id"],
                    "reviewer-loop app-server client does not support interactive server requests",
                )
                continue
            if message.get("id") == 1:
                if "error" in message:
                    raise LoopError(f"codex app-server initialize failed: {message['error']}")
                initialized = True

        _send_json_rpc_request(
            process.stdin,
            2,
            build_thread_start_request(
                cwd=repo,
                sandbox=sandbox_mode,
                model=model,
            ),
        )

        thread_id: str | None = None
        while thread_id is None:
            message = next_message()
            if _is_server_request(message):
                _send_json_rpc_error(
                    process.stdin,
                    message["id"],
                    "reviewer-loop app-server client does not support interactive server requests",
                )
                continue
            if message.get("id") == 2:
                if "error" in message:
                    raise LoopError(f"codex app-server thread/start failed: {message['error']}")
                thread_id = str(message["result"]["thread"]["id"])

        _send_json_rpc_request(
            process.stdin,
            3,
            build_turn_start_request(
                thread_id=thread_id,
                prompt=prompt,
                output_schema=output_schema,
                model=model,
            ),
        )

        deltas: dict[str, list[str]] = {}
        final_text: str | None = None
        while True:
            message = next_message()
            if _is_server_request(message):
                _send_json_rpc_error(
                    process.stdin,
                    message["id"],
                    "reviewer-loop app-server client does not support interactive server requests",
                )
                continue
            if message.get("id") == 3 and "error" in message:
                raise LoopError(f"codex app-server turn/start failed: {message['error']}")

            method = message.get("method")
            params = message.get("params", {})
            if method == "item/agentMessage/delta":
                deltas.setdefault(str(params["itemId"]), []).append(str(params["delta"]))
                continue
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    item_id = str(item.get("id"))
                    item_text = str(item.get("text") or "".join(deltas.get(item_id, [])))
                    if item.get("phase") == "final_answer" or final_text is None:
                        final_text = item_text
                continue
            if method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("status") != "completed":
                    raise LoopError(f"codex app-server turn failed: {turn}")
                break

        if final_text is None:
            raise LoopError("codex app-server completed the turn without a final assistant message")
        payload = parse_json_payload(final_text)
        if not isinstance(payload, dict):
            raise LoopError("codex app-server final payload must be a JSON object")
        return ExecutionArtifacts(
            payload=payload,
            stdout_log=_format_line_log(stdout_lines),
            stderr_log=_format_line_log(stderr_lines),
        )
    except queue.Empty as error:
        raise LoopError(f"codex app-server turn timed out after {timeout_seconds}s") from error
    finally:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)


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


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _format_location(record: Any) -> str:
    file_name = _record_value(record, "file", "<unknown file>")
    line = _record_value(record, "line")
    return f"{file_name}:{line}" if line is not None else str(file_name)


def _format_test_status(result: dict[str, Any]) -> str:
    status = "passed" if result.get("returncode") == 0 else f"failed ({result.get('returncode')})"
    return f"{result.get('command', '<unknown command>')}: {status}"


def build_manager_closeout(
    scope: ScopeResolution,
    *,
    verdict: str,
    round_records: list[dict[str, Any]],
) -> str:
    lines = [
        "# Reviewer Loop Manager Closeout",
        "",
        f"- Final verdict: `{verdict}`",
        f"- Scope: {scope.description} (`{scope.effective}`)",
        f"- Baseline: `{scope.baseline_sha}`",
        "",
        "## Issues And Fixes",
        "",
    ]

    issue_count = 0
    for round_record in round_records:
        findings = list(round_record.get("findings", []))
        if not findings:
            continue

        fixes = list(round_record.get("fixes", []))
        test_results = list(round_record.get("test_results", []))
        for finding in findings:
            issue_count += 1
            title = _record_value(finding, "title", "Untitled finding")
            severity = _record_value(finding, "severity", "unknown")
            category = _record_value(finding, "category", "unknown")
            detail = str(_record_value(finding, "detail", "")).strip()

            lines.extend(
                [
                    f"### Round {round_record.get('round')}: {title}",
                    "",
                    f"- Location: `{_format_location(finding)}`",
                    f"- Severity: `{severity}`",
                    f"- Category: `{category}`",
                    "",
                    "Why this was an issue:",
                    "",
                    detail or "The reviewers did not provide additional issue detail.",
                    "",
                    "How it was fixed:",
                    "",
                ]
            )

            if fixes:
                for fix_index, fix in enumerate(fixes, start=1):
                    summary = str(fix.get("summary") or "Fixer returned no summary.").strip()
                    notes = str(fix.get("notes") or "").strip()
                    lines.append(f"{fix_index}. {summary}")
                    if notes:
                        lines.append(f"   {notes}")
            else:
                lines.append("No automated fix was applied for this finding.")

            lines.extend(["", "Test evidence:", ""])
            if test_results:
                for result in test_results:
                    lines.append(f"- {_format_test_status(result)}")
            else:
                lines.append("- No post-fix test results were recorded for this round.")
            lines.append("")

    if issue_count == 0:
        lines.append("No review issues required fixes.")
        lines.append("")

    return "\n".join(lines)


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
