#!/usr/bin/env python3
"""Drive Claude Code sessions hosted in a dedicated tmux server, one per lane.

Subcommands (all take --lane; lanes run independently and in parallel)
  new     switch the lane to a fresh session: its next `turn` starts it (viewer follows)
  turn    send a message as a new turn of the lane's session and follow it
  answer  reply to the question Claude asked through AskUserQuestion and keep following
  wait    re-attach to the lane's running turn (e.g. after this script was killed)
  steer   add a message to the lane's turn that is running right now
  hook    internal: hook command of a driven session (hook event JSON on stdin)

Each lane is a window of tmux session `claude` on server `agy-claude`, running the
interactive Claude Code TUI; the user watches with `claude watch` (claude_view.py).
Hooks injected through --settings append every prompt, tool call, subagent,
question and stop to the lane's events.jsonl. `turn`, `answer` and `wait` follow
that log and exit when the work is done (0, or 1 on error) or when Claude asks a
question (77, saved in question.json). Subagents run in the background, so the
root turn may stop and be woken again when they finish: the work is done only at
a Stop with no background task left. Run them with IsDaemon=false so the exit
wakes the Antigravity main agent. While the main agent prepares the answer the
hook keeps the AskUserQuestion call open; `answer` hands the reply to it.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

TMUX = shutil.which("tmux") or os.path.expanduser("~/.local/bin/tmux")
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
SERVER = "agy-claude"  # tmux -L socket of the hosting server
SESSION = "claude"  # its tmux session; every lane is one window of it
CONTROL = Path.home() / ".cache" / "agy-dual-model-pipeline" / "claude"
MODEL = "claude-opus-5-5"
EFFORT = "xhigh"
EXIT_BUSY = 75
EXIT_IDLE = 76
EXIT_QUESTION = 77
EXIT_MODE = 2
# The hook holds AskUserQuestion open this long. Past it Claude falls back to its
# own picker, which only a person in the viewer can answer (reported as BLOCKED).
QUESTION_TIMEOUT = 86400
# Seconds until Claude must confirm it received a message: a fresh process boots first.
ACK_TIMEOUT_START = 90
ACK_TIMEOUT_LIVE = 20
# A parent Claude Code session exports these to its tools; when this script runs
# under Claude Code, the driven session must not take over that identity.
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                       "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
                       "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
                       "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PLUGIN_DATA")
# Appended to the system prompt, so every turn knows how to reach the main agent.
ASK_RULE = ("你由反重力主代理通过脚本驱动，旁观的人不会替你回答。遇到必须由主代理裁决才能继续的问题"
            "（裁决单与源码不符、需要修改裁决单之外的代码或顺手修同类问题、需要超出允许范围的文件、契约有歧义等），"
            "严禁自作主张擅自修改未经裁决的内容：不要每发现一个问题就立刻单独提问，也不必死等全部结束才问——"
            "把问题攒到合理数量（例如 2～4 个，或当前小批核查告一段落时）即可调用 AskUserQuestion 批量提问（写清证据和可选方案），"
            "等主代理裁决批准后再动刀；子代理遇到时先汇报给主会话。不阻塞的已授权工作照常推进。"
            "当任务涉及多个互不重叠的文件分片时，请合理并发派发 2～4 个子代理（最多不超过 4 个）按互不重叠的文件分片并行核查上下文与执行，避免根会话单线程串行耗时。")
MODE_RULES = {
    "review": "本会话是只读复审：写文件工具已禁用，也不要用 Bash 修改文件、提交或改动 git 状态；只读代码、跑只读命令和测试，给出结论。",
    "change": "本会话允许修改代码，但不提交、不推送。",
}
# Viewer keys: F2 toggles keyboard input to Claude (off by default, so a stray
# keystroke cannot reach a session), F3 cycles lanes, the wheel scrolls back,
# Ctrl-C detaches. Lane mode and session are window options shown in the status line.
SERVER_OPTIONS = (
    ("set", "-g", "mouse", "on"),
    ("set", "-g", "history-limit", "50000"),
    ("set", "-wg", "remain-on-exit", "on"),
    ("set", "-s", "focus-events", "on"),
    ("set", "-as", "terminal-features", ",*:RGB"),
    ("set", "-g", "status-left-length", "40"),
    ("set", "-g", "status-left", " Claude #{@agy_mode} #{=8:@agy_session} "),
    ("set", "-g", "status-right-length", "90"),
    ("set", "-g", "status-right", "#{?pane_input_off,只看（F2 打开键盘）,键盘已打开（F2 关闭）} | F3 切换车道 | 滚轮翻看历史 | Ctrl-C 退出观看 "),
    ("bind", "-n", "F2", "if", "-F", "#{pane_input_off}", "select-pane -e", "select-pane -d"),
    ("bind", "-n", "F3", "next-window"),
    ("bind", "-n", "C-c", "detach-client"),
    # The Antigravity terminal snapshots and replays its xterm.js when the view remounts
    # (e.g. switching tabs), which keeps mouse tracking but drops SGR encoding (?1006h).
    # X10 reports then go to xterm's onBinary, which Antigravity never forwards, so the
    # wheel stops working. tmux resends all mouse modes when they change, so cycle the
    # option whenever the terminal regains focus; the second step runs in a later loop
    # iteration, otherwise tmux sees no change and sends nothing.
    ("set-hook", "-g", "client-focus-in", 'set -g mouse off ; run -C -d 0.1 "set -g mouse on"'),
)


class Lane:
    """One independently driven Claude session: a window of the tmux session plus its control directory."""

    def __init__(self, name):
        self.name = name
        self.window = "{}:={}".format(SESSION, name)
        self.control = CONTROL / name
        self.events = self.control / "events.jsonl"
        self.active = self.control / "active.json"
        self.question = self.control / "question.json"
        self.answer = self.control / "answer.json"
        self.current = self.control / "session.json"


def lane_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
        raise argparse.ArgumentTypeError("lane names are lowercase letters, digits and '-'")
    return value


def tmux(*args):
    return subprocess.run([TMUX, "-L", SERVER] + list(args), check=True, capture_output=True, text=True).stdout


def session_exists():
    return subprocess.run([TMUX, "-L", SERVER, "has-session", "-t", SESSION], capture_output=True).returncode == 0


def window_exists(lane):
    return session_exists() and lane.name in tmux("list-windows", "-t", SESSION, "-F", "#{window_name}").split()


def claude_running(lane):
    # remain-on-exit keeps the pane after Claude exits; pane_dead tells.
    return window_exists(lane) and tmux("display", "-p", "-t", lane.window, "#{pane_dead}").strip() == "0"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def append_event(lane, record):
    # One O_APPEND write per line, so concurrent hook processes never interleave.
    fd = os.open(str(lane.events), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


def read_events(lane, offset):
    """Complete event lines from `offset`, each with the offset just after it."""
    with open(lane.events, "rb") as log:
        log.seek(offset)
        data = log.read()
    events = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # still being written
        offset += len(line)
        events.append((json.loads(line), offset))
    return events


# ---------------------------------------------------------------- hook side

RISKY_BASH_RE = re.compile(
    r"(?:^|[;&|()\s`])(?:rm|rmdir|unlink|shred|sudo|mkfs|dd)(?=\s)"
    r"|\bgit\s+(?:clean|reset|push|checkout\s+--|restore)(?=\s|$)"
)


def summarize(tool, tool_input):
    if tool == "Bash":
        return "$ " + tool_input["command"].partition("\n")[0][:200]
    for key in ("file_path", "notebook_path", "pattern", "url", "query", "description", "skill"):
        if key in tool_input:
            return "[{}] {}".format(tool, str(tool_input[key])[:200])
    return "[{}]".format(tool)


def is_safe_permission_request(tool, tool_input):
    """Return True when a PermissionRequest is a shell-parser false positive with no destructive command."""
    if tool != "Bash":
        return False
    command = (tool_input or {}).get("command") or ""
    return not bool(RISKY_BASH_RE.search(command))


def wait_for_answer(lane, req_id, questions):
    """Queue on `question.json` and block until `answer` delivers the main agent's reply for `req_id`."""
    while open_question(lane):
        time.sleep(0.5)
    write_json(lane.question, {"tool_use_id": req_id, "questions": questions, "pid": os.getpid()})
    append_event(lane, {"ev": "question", "id": req_id})
    while not (lane.answer.exists() and read_json(lane.answer)["tool_use_id"] == req_id):
        time.sleep(0.5)
    reply = read_json(lane.answer)["text"]
    lane.answer.unlink()
    lane.question.unlink()
    append_event(lane, {"ev": "answered"})
    return reply


def ask(lane, payload):
    """Hold the AskUserQuestion call open until `answer` hands over the main agent's reply."""
    questions = payload["tool_input"]["questions"]
    reply = wait_for_answer(lane, payload["tool_use_id"], questions)
    # One reply serves every question of the call, as with Codex's request_user_input.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "updatedInput": {"questions": questions, "answers": {q["question"]: reply for q in questions}}}},
        ensure_ascii=False))


def permission_request(lane, payload):
    """Auto-allow shell-parser false positives; hold risky PermissionRequests open for main agent `answer`."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if is_safe_permission_request(tool, tool_input):
        append_event(lane, {"ev": "tool", "agent": payload.get("agent_id"),
                            "text": "[auto-allow] " + summarize(tool, tool_input)})
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }}, ensure_ascii=False))
        return
    req_id = payload.get("tool_use_id") or ("perm_" + uuid.uuid4().hex[:12])
    detail = tool_input.get("command") if tool == "Bash" else json.dumps(tool_input, ensure_ascii=False)
    questions = [{
        "header": "PermissionRequest ({})".format(tool),
        "question": "Claude 触发高危权限确认，请主代理裁决是否放行：\n{}".format(detail),
        "options": [
            {"label": "allow", "description": "放行该命令/操作（回复 allow）"},
            {"label": "deny", "description": "拦截该命令/操作（回复 deny: 原因）"},
        ],
    }]
    reply = wait_for_answer(lane, req_id, questions).strip()
    lower = reply.lower()
    if lower.startswith(("deny", "no", "block", "reject")) or reply.startswith(("拒绝", "拦截", "不")):
        reason = reply.partition(":")[2].strip() or reply
        decision = {"behavior": "deny", "message": reason}
    else:
        decision = {"behavior": "allow"}
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": decision,
    }}, ensure_ascii=False))


def open_question(lane):
    """The question a hook is holding open, or None. Claude kills a hook at its timeout
    and falls back to its own picker; the dead hook's file is then cleared."""
    if not lane.question.exists():
        return None
    question = read_json(lane.question)
    try:
        os.kill(question["pid"], 0)
    except ProcessLookupError:
        lane.question.unlink()
        return None
    return question


def hook(lane):
    payload = json.load(sys.stdin)
    event = payload["hook_event_name"]
    if event == "UserPromptSubmit":
        if not lane.active.exists():
            # A message nobody follows (typed in the viewer): `wait` can follow it.
            write_json(lane.active, {"cursor": lane.events.stat().st_size})
        append_event(lane, {"ev": "prompt", "text": payload["prompt"][:300]})
    elif event == "PreToolUse":  # matcher limits it to AskUserQuestion
        ask(lane, payload)
    elif event == "PermissionRequest":
        permission_request(lane, payload)
    elif event == "PostToolUse":
        append_event(lane, {"ev": "tool", "agent": payload.get("agent_id"),
                            "text": summarize(payload["tool_name"], payload["tool_input"])})
    elif event in ("SubagentStart", "SubagentStop"):
        append_event(lane, {"ev": event, "agent": payload["agent_id"], "type": payload["agent_type"]})
    elif event == "Stop":
        append_event(lane, {"ev": "stop", "message": payload.get("last_assistant_message") or "",
                            "background": len(payload["background_tasks"])})
    elif event == "StopFailure":
        append_event(lane, {"ev": "failure", "error": payload.get("error"),
                            "details": payload.get("error_details") or payload.get("last_assistant_message") or ""})
    elif event == "Notification" and payload["notification_type"] == "permission_prompt":
        if not open_question(lane):
            append_event(lane, {"ev": "blocked", "message": payload["message"]})


def hook_settings(lane):
    command = {"type": "command", "command": "{} {} hook --lane {}".format(
        shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)), lane.name)}
    every = [{"hooks": [command]}]
    long = [{"hooks": [dict(command, timeout=QUESTION_TIMEOUT)]}]
    return {"hooks": {
        "UserPromptSubmit": every,
        "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [dict(command, timeout=QUESTION_TIMEOUT)]}],
        "PermissionRequest": long,
        "PostToolUse": every,
        "SubagentStart": every,
        "SubagentStop": every,
        "Stop": every,
        "StopFailure": every,
        "Notification": every,
    }}


# ---------------------------------------------------------------- driver side

def launch(lane, session, prompt, resume):
    """Start Claude in the lane's window on `session`, with this turn's message as its startup prompt."""
    rules = ASK_RULE + "\n" + MODE_RULES[session["mode"]]
    if session["files"]:
        rules += "\n允许修改的文件范围：" + session["files"]
    settings = lane.control / "settings.json"
    write_json(settings, hook_settings(lane))
    argv = ["/usr/bin/env"]
    for name in PARENT_SESSION_VARS:
        argv += ["-u", name]
    # Claude picks its fullscreen renderer from a remote feature flag on some launches. That
    # renderer draws on the alternate screen and captures the mouse, so tmux keeps no history
    # and the viewer's wheel goes to the input-off pane and is dropped. The inline renderer
    # leaves the transcript in tmux scrollback, where the wheel opens copy mode.
    argv += ["COLORTERM=truecolor", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1",
             CLAUDE, "--resume" if resume else "--session-id", session["id"],
             "--name", "agy-{}-{}".format(lane.name, session["id"][:8]), "--settings", str(settings),
             "--model", MODEL, "--effort", EFFORT, "--permission-mode", "bypassPermissions",
             "--append-system-prompt", rules]
    if session["mode"] == "review":
        argv += ["--disallowedTools", "Edit,Write,NotebookEdit"]
    # The TUI takes keyboard input only seconds after it boots, so the first message
    # goes in as the startup prompt. `--` also ends the variadic --disallowedTools.
    command = " ".join(shlex.quote(arg) for arg in argv) + ' -- "$(cat {})"'.format(shlex.quote(str(prompt)))
    if window_exists(lane):
        tmux("respawn-pane", "-k", "-t", lane.window, "-c", session["cwd"], command)
    elif session_exists():
        tmux("new-window", "-d", "-t", SESSION + ":", "-n", lane.name, "-c", session["cwd"], command)
    else:
        # Options go first in the same tmux invocation: history-limit only applies to panes
        # created after it, and remain-on-exit must be on before Claude could exit and take
        # the pane along.
        args = ["start-server"]
        for option in SERVER_OPTIONS:
            args += [";"] + list(option)
        args += [";", "new-session", "-d", "-s", SESSION, "-n", lane.name, "-x", "200", "-y", "50",
                 "-c", session["cwd"], command]
        tmux(*args)
    tmux("select-pane", "-d", "-t", lane.window)
    tmux("set", "-w", "-t", lane.window, "@agy_mode", session["mode"])
    tmux("set", "-w", "-t", lane.window, "@agy_session", session["id"])


def keyboard_command(lane, command):
    """Run a tmux command that types into the lane's pane, keeping the viewer's F2 keyboard state."""
    tmux("if", "-F", "-t", lane.window, "#{pane_input_off}",
         "select-pane -e -t {0} ; {1} ; select-pane -d -t {0}".format(lane.window, command), command)


def paste(lane, prompt):
    """Type a message file into Claude as one bracketed paste and submit it."""
    buffer = "agy-" + uuid.uuid4().hex[:8]
    tmux("load-buffer", "-b", buffer, str(prompt))
    keyboard_command(lane, "paste-buffer -p -r -d -b {} -t {}".format(buffer, lane.window))
    # Claude needs a moment to take in a large paste before Enter submits it.
    time.sleep(max(0.5, min(3.0, prompt.stat().st_size / 40000)))
    keyboard_command(lane, "send-keys -t {} Enter".format(lane.window))


def acknowledged(lane, offset, timeout):
    """Wait until the UserPromptSubmit hook confirms a message arrived after `offset`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(event["ev"] == "prompt" for event, _ in read_events(lane, offset)):
            return True
        time.sleep(0.5)
    return False


def print_question(questions):
    print("\n===== CLAUDE ASKS (turn still running, waiting for `answer`) =====")
    for question in questions:
        print("[{}] {}".format(question["header"], question["question"]))
        for option in question["options"]:
            print("  - {}: {}".format(option["label"], option["description"]))


def follow(lane, cursor, answered=None):
    """Print the lane's events from `cursor` until the work completes (0/1), Claude asks (77) or gets stuck (1).

    On a question or block the cursor is saved before that event, so `wait` reports
    it again while it is still open; `answered` is the question `answer` just replied to.
    """
    agents = {}  # agent_id -> agent_type, to label subagent tool calls
    last_check = time.time()
    while True:
        events = read_events(lane, cursor)
        for index, (event, after) in enumerate(events):
            kind = event["ev"]
            if kind == "prompt":
                if event["text"].startswith("<task-notification>"):
                    print("[background task finished, root turn resumed]")
                else:
                    print("[message] " + event["text"].partition("\n")[0][:120])
            elif kind == "tool":
                print(("[{}] ".format(agents.get(event["agent"], "subagent")) if event["agent"] else "") + event["text"])
            elif kind == "SubagentStart":
                agents[event["agent"]] = event["type"]
                print("[subagent {} started] {}".format(event["type"], event["agent"]))
            elif kind == "SubagentStop":
                print("[subagent {} finished] {}".format(event["type"], event["agent"]))
            elif kind == "question":
                question = open_question(lane)
                if event["id"] != answered and question and question["tool_use_id"] == event["id"]:
                    write_json(lane.active, {"cursor": cursor})
                    print_question(question["questions"])
                    return EXIT_QUESTION
            elif kind == "answered":
                print("ANSWER DELIVERED")
            elif kind == "blocked":
                tail = "\n".join(tmux("capture-pane", "-p", "-t", lane.window).splitlines()[-25:]) if window_exists(lane) else ""
                if index == len(events) - 1 or "Do you want to proceed?" in tail:
                    write_json(lane.active, {"cursor": after})
                    print("BLOCKED: Claude 在界面里等人处理（{}）。\n{}".format(event["message"], tail))
                    return 1
            elif kind == "stop":
                if event["background"]:
                    if event["message"]:
                        print(event["message"])
                    print("[root turn paused, {} background task(s) still running]".format(event["background"]))
                else:
                    lane.active.unlink(missing_ok=True)
                    print("\n===== TURN COMPLETED lane={} =====".format(lane.name))
                    print(event["message"] or "<no final assistant message>")
                    return 0
            elif kind == "failure":
                lane.active.unlink(missing_ok=True)
                print("\n===== TURN FAILED lane={} error={} =====".format(lane.name, event["error"]))
                print(event["details"])
                return 1
            sys.stdout.flush()
            cursor = after
        if not events:
            if time.time() - last_check > 2:
                last_check = time.time()
                if not claude_running(lane):
                    lane.active.unlink(missing_ok=True)
                    print("CLAUDE EXITED: the Claude process of lane {} is gone; its next `turn` resumes the session.".format(lane.name))
                    return 1
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("new", "turn", "answer", "wait", "steer", "hook"))
    parser.add_argument("--lane", type=lane_name, required=True, help="independent session to drive, e.g. verify or fix")
    parser.add_argument("--cwd", help="new: repository root the next session works in")
    parser.add_argument("--mode", choices=("review", "change"), help="turn: mode of the session (fixed per session)")
    parser.add_argument("--files", default="", help="turn --mode change: |-separated repo-relative paths")
    parser.add_argument("--prompt-file", help="turn/steer: message text file; answer: reply text file")
    args = parser.parse_args()
    lane = Lane(args.lane)

    if args.command == "hook":
        hook(lane)
        return 0

    lane.control.mkdir(parents=True, exist_ok=True)

    if args.command == "new":
        if not args.cwd:
            parser.error("new requires --cwd")
        if lane.active.exists():
            print("BUSY: a turn is still running on lane {}; wait for it before switching.".format(lane.name))
            return EXIT_BUSY
        (lane.control / "cwd").write_text(args.cwd + "\n", encoding="utf-8")
        lane.current.unlink(missing_ok=True)
        print("NEXT TURN OF LANE {} STARTS A NEW SESSION in {}".format(lane.name, args.cwd))
        return 0

    if args.command in ("answer", "wait"):
        if not lane.active.exists():
            print("IDLE: no turn is running on lane {}.".format(lane.name))
            return EXIT_IDLE
        answered = None
        if args.command == "answer":
            question = open_question(lane)
            if not question:
                print("NO QUESTION: Claude has no open question on lane {}; use `steer` to add information "
                      "to the running turn, or `wait` to see why it is stuck.".format(lane.name))
                return EXIT_IDLE
            answered = question["tool_use_id"]
            write_json(lane.answer, {"tool_use_id": answered, "text": Path(args.prompt_file).read_text(encoding="utf-8")})
        return follow(lane, read_json(lane.active)["cursor"], answered)

    prompt = Path(args.prompt_file).resolve()

    if args.command == "steer":
        if not lane.active.exists():
            print("IDLE: no turn is running on lane {}; send it with `turn` instead.".format(lane.name))
            return EXIT_IDLE
        offset = lane.events.stat().st_size
        paste(lane, prompt)
        if not acknowledged(lane, offset, ACK_TIMEOUT_LIVE):
            print("STEER NOT ACKNOWLEDGED: Claude did not receive the message; check lane {} in `claude watch`.".format(lane.name))
            return 1
        print("STEERED lane={}".format(lane.name))
        return 0

    if not args.mode:
        parser.error("turn requires --mode")
    if lane.active.exists():
        print("BUSY: a turn is already running on lane {}; use `steer` to add to it, "
              "or `wait`/`answer` to follow it.".format(lane.name))
        return EXIT_BUSY
    session = read_json(lane.current) if lane.current.exists() else None
    if session and (session["mode"], session["files"]) != (args.mode, args.files):
        print("MODE MISMATCH: lane {} runs --mode {} --files '{}'; switch with `new` first.".format(
            lane.name, session["mode"], session["files"]))
        return EXIT_MODE
    if session and claude_running(lane):
        offset = lane.events.stat().st_size
        write_json(lane.active, {"cursor": offset})
        paste(lane, prompt)
        timeout = ACK_TIMEOUT_LIVE
    else:
        # No session yet, or its Claude exited: then the same conversation resumes.
        resume = session is not None
        if not resume:
            session = {"id": str(uuid.uuid4()), "cwd": (lane.control / "cwd").read_text(encoding="utf-8").strip(),
                       "mode": args.mode, "files": args.files}
            write_json(lane.current, session)
            with open(lane.control / "sessions.log", "a", encoding="utf-8") as log:
                log.write("{} {} {}\n".format(session["id"], session["mode"], session["cwd"]))
        lane.events.write_text("", encoding="utf-8")
        offset = 0
        write_json(lane.active, {"cursor": offset})
        launch(lane, session, prompt, resume)
        timeout = ACK_TIMEOUT_START
    # The viewer follows the lane whose turn started last.
    tmux("select-window", "-t", lane.window)
    if not acknowledged(lane, offset, timeout):
        lane.active.unlink(missing_ok=True)
        print("NOT ACKNOWLEDGED: Claude did not receive the message (stuck in a startup dialog?). Handle it on "
              "lane {} in `claude watch` (F2 opens the keyboard), then send the turn again.".format(lane.name))
        return 1
    print("TURN STARTED lane={} session={} mode={}".format(lane.name, session["id"], args.mode))
    return follow(lane, offset)


if __name__ == "__main__":
    sys.exit(main())
