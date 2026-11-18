#!/usr/bin/env python3
"""Antigravity hook gate enforcing full reload of activated skills after context compaction.

Single source of truth:
  ~/.gemini/antigravity/brain/<conversationId>/.system_generated/logs/transcript.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
CONV_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
SKILL_TAG_RE = re.compile(r"The path to the skill file is:\s*\n([^\n<]+)")
READ_TOOL_PATH_ARGS = {"read_file": "Path", "view_file": "AbsolutePath"}
READ_FILE_TS_PREFIXES = ("Created At: ", "Completed At: ")
READ_FILE_LINES_RE = re.compile(r"^Showing lines (\d+)-(\d+) of (\d+)$")
READ_FILE_PARTIAL_RE = re.compile(
    r"^Partial read \(lines (\d+)-(\d+) of (\d+)\)\. Showing bytes (\d+)-(\d+) of (\d+) in this line range"
)
VIEW_FILE_FULL_MARKER = "The above content shows the entire, complete file contents of the requested file."
INPUT_STEP_TYPES = ("USER_INPUT", "SYSTEM_MESSAGE")
DELIVERY_TOOL_NAMES = ("send_message", "post_message")

PIPELINE_SKILL_PATH = str(Path.home() / ".gemini/config/skills/agy-dual-model-pipeline/SKILL.md")
PIPELINE_BOUND_FILES = (
    str(Path.home() / ".codex/skills/认知一致性/SKILL.md"),
    str(Path.home() / ".gemini/config/skills/agy-dual-model-pipeline/references/blacklist.md"),
)


def unquote_arg(val: Any) -> Any:
    """Unquote a JSON-encoded string argument from transcript tool_calls."""
    if isinstance(val, str) and len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        return json.loads(val)
    return val


def extract_tool_path(tc: dict[str, Any]) -> str | None:
    """Return the target path of a read_file (Path) or view_file (AbsolutePath) call, else None."""
    arg_name = READ_TOOL_PATH_ARGS.get(tc.get("name", ""))
    if arg_name is None:
        return None
    raw = tc.get("args", {}).get(arg_name)
    val = unquote_arg(raw)
    return val if isinstance(val, str) else None


def iter_tool_results(steps: list[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Yield (tool_call, GENERIC result step or None) for every tool call in `steps`."""
    results = {int(s["step_index"]): s for s in steps if s.get("type") == "GENERIC" and "step_index" in s}
    for step in steps:
        if step.get("type") == "PLANNER_RESPONSE" and "step_index" in step:
            base = int(step["step_index"]) + 1
            for k, tc in enumerate(step.get("tool_calls", ())):
                yield tc, results.get(base + k)


def iter_done_reads(steps: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Yield (target_path, tool_call, result) for every read_file/view_file call whose result is DONE."""
    for tc, result in iter_tool_results(steps):
        path = extract_tool_path(tc)
        if path is not None and result is not None and result.get("status") == "DONE":
            yield path, tc, result


def covers_half_open(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """Return True iff the union of half-open intervals `spans` covers `[start, end)`."""
    cursor = start
    for lo, hi in sorted(spans):
        if hi <= cursor:
            continue
        if lo > cursor:
            return False
        cursor = hi
        if cursor >= end:
            return True
    return cursor >= end


def extract_read_file_header(content: str) -> str:
    """Return the first non-timestamp header line of a read_file step content."""
    for line in content.splitlines():
        if not line.startswith(READ_FILE_TS_PREFIXES):
            return line
    return ""


def collect_full_reads(steps: list[dict[str, Any]]) -> set[str]:
    """Return target paths whose DONE read calls after CHECKPOINT cover 100% of lines and bytes."""
    complete: set[str] = set()
    totals_by_path: dict[str, int] = {}
    line_spans_by_path: dict[str, list[tuple[int, int]]] = {}
    byte_spans_by_range: dict[tuple[str, int, int, int], list[tuple[int, int]]] = {}

    for path, tc, result in iter_done_reads(steps):
        if path in complete:
            continue
        content = result.get("content", "")
        if tc.get("name") != "read_file":
            if content.rstrip().endswith(VIEW_FILE_FULL_MARKER):
                complete.add(path)
            continue

        header = extract_read_file_header(content)
        if not header:
            continue

        m_lines = READ_FILE_LINES_RE.match(header)
        if m_lines is not None:
            s_line, e_line, total_lines = (int(x) for x in m_lines.groups())
            if totals_by_path.get(path) != total_lines:
                totals_by_path[path] = total_lines
                line_spans_by_path[path] = []
            line_spans_by_path[path].append((s_line, e_line + 1))
        else:
            m_part = READ_FILE_PARTIAL_RE.match(header)
            if m_part is None:
                continue
            s_line, e_line, total_lines, b_start, b_end, total_bytes = (int(x) for x in m_part.groups())
            if totals_by_path.get(path) != total_lines:
                totals_by_path[path] = total_lines
                line_spans_by_path[path] = []
            b_key = (path, s_line, e_line, total_bytes)
            b_spans = byte_spans_by_range.setdefault(b_key, [])
            b_spans.append((b_start, b_end))
            if covers_half_open(b_spans, 0, total_bytes):
                line_spans_by_path[path].append((s_line, e_line + 1))
            else:
                continue

        if covers_half_open(line_spans_by_path[path], 1, totals_by_path[path] + 1):
            complete.add(path)

    return complete


def collect_required_files(steps: list[dict[str, Any]]) -> list[str]:
    """Extract deduplicated activated SKILL.md files and pipeline-bound specs."""
    required_files: list[str] = []

    def add_skill(path_str: str) -> None:
        if (
            not (path_str.endswith("/SKILL.md") or path_str in PIPELINE_BOUND_FILES)
            or "/antigravity/builtin/skills/" in path_str
            or path_str in required_files
        ):
            return
        required_files.append(path_str)
        if path_str == PIPELINE_SKILL_PATH:
            for bound_path in PIPELINE_BOUND_FILES:
                add_skill(bound_path)

    for step in steps:
        if step.get("type") in INPUT_STEP_TYPES:
            for match in SKILL_TAG_RE.findall(step.get("content", "")):
                add_skill(match.strip())
    for path, _, _ in iter_done_reads(steps):
        add_skill(path)

    return required_files


def delivered_before_checkpoint(steps: list[dict[str, Any]], last_cp_idx: int) -> bool:
    """Return True iff the last turn before CHECKPOINT delivered its report (DONE) and no user/task input followed."""
    if any(
        s.get("type") in INPUT_STEP_TYPES
        and "Stop hook blocked termination:" not in s.get("content", "")
        for s in steps[last_cp_idx + 1 :]
    ):
        return False
    last_planner_idx = next(
        (i for i in range(last_cp_idx - 1, -1, -1) if steps[i].get("type") == "PLANNER_RESPONSE"),
        last_cp_idx,
    )
    return any(
        tc.get("name") in DELIVERY_TOOL_NAMES and result is not None and result.get("status") == "DONE"
        for tc, result in iter_tool_results(steps[last_planner_idx:last_cp_idx])
    )


def evaluate_steps(event: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate hook decision for a validated event against loaded transcript steps."""
    last_cp_idx = next(
        (i for i in range(len(steps) - 1, -1, -1) if steps[i].get("type") == "CHECKPOINT"),
        None,
    )
    if last_cp_idx is None:
        return {"decision": "allow"} if event == "PreToolUse" else {}

    post_cp_steps = steps[last_cp_idx + 1 :]
    if event == "Stop":
        stop_blocks = sum(
            1
            for s in post_cp_steps
            if s.get("type") == "SYSTEM_MESSAGE" and "Stop hook blocked termination:" in s.get("content", "")
        )
        if stop_blocks >= 1:
            return {}

    if event == "PreToolUse":
        last_planner_idx = next(
            (i for i in range(len(steps) - 1, last_cp_idx, -1) if steps[i].get("type") == "PLANNER_RESPONSE"),
            last_cp_idx + 1,
        )
        completed_steps = steps[last_cp_idx + 1 : last_planner_idx]
    elif delivered_before_checkpoint(steps, last_cp_idx):
        return {}
    else:
        completed_steps = post_cp_steps

    full_reads = collect_full_reads(completed_steps)
    unread_files = [f for f in collect_required_files(steps) if f not in full_reads]
    if not unread_files:
        return {"decision": "allow"} if event == "PreToolUse" else {}

    bullets = "\n".join(f"- {f}" for f in unread_files)
    if event == "PreInvocation":
        return {
            "injectSteps": [
                {
                    "ephemeralMessage": (
                        "【Compaction Skill Gate 强制门禁】检测到当前会话发生了上下文压缩（CHECKPOINT），"
                        "此前读取的 Skill 及核心规范已从活跃上下文中丢失！在调用任何其他工具或回复用户之前，"
                        "你本轮必须且只能调用 read_file（或 view_file）读取以下文件的完整全文"
                        f"（若超单次行/字节上限须继续分段读齐全文，严禁同轮混调其他工具）：\n{bullets}"
                    )
                }
            ]
        }
    if event == "PreToolUse":
        return {
            "decision": "deny",
            "reason": (
                "【Compaction Skill Gate 硬件拦截】检测到上下文压缩（CHECKPOINT）后你尚未完整重读本会话已激活的 Skill 及核心规范文件"
                "（被字节/行上限截断的半截读取不算完成，且严禁在重读结果返回前同轮抢跑其他工具）。"
                f"请先调用 read_file（或 view_file）读齐以下文件的完整全文后再调用其他工具：\n{bullets}"
            ),
        }
    return {
        "decision": "continue",
        "reason": (
            "【Compaction Skill Gate 强制继续】检测到上下文压缩（CHECKPOINT）后你尚未完整重读本会话已激活的 Skill 及核心规范文件，"
            f"请立即调用 read_file（或 view_file）读齐以下文件的完整全文：\n{bullets}"
        ),
    }


def handle_hook(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate hook event and payload, short-circuit non-gated calls, and evaluate transcript."""
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not CONV_ID_RE.fullmatch(conversation_id):
        raise ValueError(f"Invalid conversationId: {conversation_id!r}")

    if event == "PreToolUse":
        tool_call = payload.get("toolCall")
        tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
        if tool_name in READ_TOOL_PATH_ARGS:
            return {"decision": "allow"}
    elif event == "Stop":
        if payload.get("terminationReason") != "NO_TOOL_CALL":
            return {}
    elif event != "PreInvocation":
        raise ValueError(f"Unsupported hook event: {event!r}")

    transcript_path = BRAIN_DIR / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_path.is_file():
        return {"decision": "allow"} if event == "PreToolUse" else {}

    steps = [
        json.loads(line)
        for line in transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return evaluate_steps(event, steps)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: compaction_skill_gate.py <PreInvocation|PreToolUse|Stop>")
    result = handle_hook(sys.argv[1], json.load(sys.stdin))
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
