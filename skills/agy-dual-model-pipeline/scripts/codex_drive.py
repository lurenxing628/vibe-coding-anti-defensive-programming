#!/usr/bin/env python3
"""Drive the shared Codex session through the local app-server daemon.

Subcommands
  new     switch to a fresh thread: the next `turn` starts it (viewer follows)
  turn    send a message as a new turn on the current thread and follow it
  answer  reply to the question Codex asked in the running turn and keep following
  wait    re-attach to the running turn (e.g. after this script was killed)
  steer   append a message to the turn that is running right now

Every subcommand takes --lane: each lane drives its own thread with its own state,
so lanes run in parallel. The viewer follows the lane whose turn started last.

`turn`, `answer` and `wait` follow the running turn and exit when it completes
(0, or 1 on turn error) or when Codex asks a question through
request_user_input (77, question saved in question.json). Run them with
IsDaemon=false so the exit wakes the Antigravity main agent. While Codex waits
for the answer the turn stays alive in the daemon; `answer` resumes the thread,
the daemon re-sends the pending question, and the reply goes back to it.
"""
import argparse
import base64
import json
import os
import re
import select
import socket
import struct
import sys
from pathlib import Path

SOCKET = os.path.expanduser("~/.codex/app-server-control/app-server-control.sock")
# Shared with codex_view.py: per lane, which thread is current and whether a turn is
# running; latest_lane names the lane whose turn started last.
BASE = Path.home() / ".cache" / "agy-dual-model-pipeline" / "codex"
MODEL = "gpt-6-astra-minor"
EFFORT = "ultra"
EXIT_BUSY = 75
EXIT_IDLE = 76
EXIT_QUESTION = 77
# Sent on the line after the guard directive of every turn, so each turn knows
# how to reach the main agent. request_user_input is root-thread only.
ASK_RULE = ("遇到必须由主代理裁决才能继续的问题（裁决单与源码不符、需要超出允许范围的文件、契约有歧义等），"
            "不要猜，也不要自行扩大范围：由根代理调用 request_user_input 提问，写清证据和可选方案；"
            "subAgent 遇到时先汇报给根代理。不阻塞的工作照常推进。")


class Client:
    """JSON-RPC over the daemon's control socket, which speaks WebSocket."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(SOCKET)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n").format(key).encode())
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.recv()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade refused: " + head.decode(errors="replace"))
        self.next_id = 0
        self.request("initialize", {"clientInfo": {"name": "agy-dual-model-pipeline", "version": "1"}})
        self.notify("initialized")

    def recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise RuntimeError("app-server connection closed")
        return chunk

    def take(self, size):
        while len(self.buffer) < size:
            self.buffer += self.recv()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def write_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", len(payload))
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", len(payload))
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, message):
        self.write_frame(0x1, json.dumps(message, ensure_ascii=False).encode())

    def read(self):
        parts = []
        while True:
            first, second = self.take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self.take(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self.take(8))[0]
            payload = self.take(size)
            opcode = first & 0x0F
            if opcode == 0x9:
                self.write_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise RuntimeError("app-server closed the websocket")
            if opcode in (0x1, 0x0):
                parts.append(payload)
                if first & 0x80:
                    return json.loads(b"".join(parts))

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params, on_event=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            message = self.read()
            if message.get("id") == self.next_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError("{} failed: {}".format(method, message["error"]))
                return message["result"]
            if on_event:
                on_event(message)


def lane_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
        raise argparse.ArgumentTypeError("lane names are lowercase letters, digits and '-'")
    return value


def directive(mode, files):
    if mode == "review":
        return "$stop-that-shit review"
    if files:
        return "$stop-that-shit change\n允许修改的文件范围：" + files
    return "$stop-that-shit change"


def text_input(text):
    return [{"type": "text", "text": text}]


def show(message, family):
    """Print one line per completed item of this thread and its subAgents; the live view is the TUI in the viewer terminal."""
    if message.get("method") != "item/completed" or message["params"]["threadId"] not in family:
        return
    item = message["params"]["item"]
    kind = item["type"]
    if kind == "agentMessage":
        print(item["text"])
    elif kind == "commandExecution":
        print("$ {}  (exit {})".format(item["command"], item.get("exitCode")))
    elif kind == "fileChange":
        print("[patch] " + ", ".join(change["path"] for change in item["changes"]))
    elif kind == "collabAgentToolCall":
        family.update(item["receiverThreadIds"])
        print("[subagent {}] -> {}".format(item["tool"], ", ".join(item["receiverThreadIds"])))
    sys.stdout.flush()


def print_question(params):
    print("\n===== CODEX ASKS (turn still running, waiting for `answer`) =====")
    for question in params["questions"]:
        print("[{}] {}".format(question["header"], question["question"]))
        for option in question["options"] or ():
            print("  - {}: {}".format(option["label"], option["description"]))


def finish_from_turn_snapshot(control, turn, answer=None):
    """ Emit final output and clean up state files when a turn already finished before/during resume."""
    if (control / "active_turn").exists():
        (control / "active_turn").unlink()
    if (control / "question.json").exists():
        (control / "question.json").unlink()
    if answer is not None:
        print("NOTE: turn already completed before `answer` was delivered (e.g. TUI 120s auto-resolve timer expired).")
    messages = [item.get("text", "") for item in turn.get("items", ()) if item.get("type") == "agentMessage" and item.get("text")]
    for msg in messages[:-1]:
        print(msg)
    final = messages[-1] if messages else "<no final agent message>"
    print("\n===== TURN COMPLETED status={} error={} =====".format(turn.get("status"), turn.get("error")))
    print(final)
    return 1 if turn.get("error") else 0


def find_turn(thread_res, turn_id):
    for turn in thread_res.get("thread", {}).get("turns", ()):
        if turn.get("id") == turn_id:
            return turn
    return None


def follow(control, client, thread_id, turn_id, answer=None, buffered=()):
    """Stream the running turn until it completes or Codex asks a new question.

    `answer` is (question, text): the reply to the saved question, delivered
    when the daemon re-sends that request on this connection, or injected via
    `turn/steer` if the TUI auto-resolve timer already closed the prompt.
    """
    # The daemon also delivers other sessions' traffic on this connection;
    # only this thread (and the subAgents it spawns) is ours.
    family = {thread_id}
    final = "<no final agent message>"
    pending = list(buffered)
    while True:
        if pending:
            message = pending.pop(0)
        else:
            if answer is not None and not client.buffer:
                ready, _, _ = select.select([client.sock], [], [], 0.5)
                if not ready:
                    # The daemon did not re-send requestUserInput within 0.5s after
                    # thread/resume (e.g. TUI auto-resolve countdown already expired).
                    # Fall back to turn/steer so the running turn still gets the answer.
                    try:
                        client.request(
                            "turn/steer",
                            {
                                "threadId": thread_id,
                                "expectedTurnId": turn_id,
                                "input": text_input(answer[1]),
                            },
                            on_event=pending.append,
                        )
                        print("ANSWER STEERED (question was already auto-resolved in TUI)")
                    except Exception as exc:
                        print("NOTE: answer steer fallback failed ({})".format(exc))
                        if not any(m.get("method") == "turn/completed" and m.get("params", {}).get("turn", {}).get("id") == turn_id for m in pending):
                            res = client.request("thread/resume", {"threadId": thread_id, "excludeTurns": False}, on_event=pending.append)
                            snap = find_turn(res, turn_id)
                            if snap is not None and snap.get("status") != "inProgress":
                                return finish_from_turn_snapshot(control, snap, answer=answer)
                    if (control / "question.json").exists():
                        (control / "question.json").unlink()
                    answer = None
                    continue
            message = client.read()
        show(message, family)
        method, params = message.get("method"), message.get("params", {})
        ours = params.get("threadId") == thread_id
        if method == "item/tool/requestUserInput" and ours:
            if answer and params["itemId"] == answer[0]["itemId"]:
                client.send({"jsonrpc": "2.0", "id": message["id"],
                             "result": {"answers": {q["id"]: {"answers": [answer[1]]} for q in params["questions"]}}})
                print("ANSWER DELIVERED")
                if (control / "question.json").exists():
                    (control / "question.json").unlink()
                answer = None
                continue
            if answer:
                print("NOTE: the saved question was already answered elsewhere (e.g. in the TUI); this answer was not delivered.")
            (control / "question.json").write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
            print_question(params)
            return EXIT_QUESTION
        if "id" in message and method and ours:
            print("UNHANDLED SERVER REQUEST {}: the turn is blocked on it; handle it in `codex watch`, then run `wait`.".format(method))
            return 1
        if method == "item/completed" and params["item"]["type"] == "agentMessage" and ours:
            final = params["item"]["text"]
        if method == "turn/completed" and params["turn"]["id"] == turn_id:
            done = params["turn"]
            break
    if (control / "active_turn").exists():
        (control / "active_turn").unlink()
    if answer:
        print("NOTE: the saved question was already answered elsewhere (e.g. in the TUI); this answer was not delivered.")
        if (control / "question.json").exists():
            (control / "question.json").unlink()
    print("\n===== TURN COMPLETED status={} error={} =====".format(done["status"], done["error"]))
    print(final)
    return 1 if done["error"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("new", "turn", "answer", "wait", "steer"))
    parser.add_argument("--lane", type=lane_name, required=True, help="independent thread to drive, e.g. counter or review")
    parser.add_argument("--cwd", help="new: repository root the next thread works in")
    parser.add_argument("--mode", choices=("review", "change"), help="turn: guard mode of this turn")
    parser.add_argument("--files", default="", help="turn --mode change: |-separated repo-relative paths")
    parser.add_argument("--prompt-file", help="turn/steer: message text file; answer: reply text file")
    args = parser.parse_args()

    control = BASE / args.lane
    control.mkdir(parents=True, exist_ok=True)
    current = control / "current_thread"
    active = control / "active_turn"
    question = control / "question.json"

    if args.command == "new":
        if not args.cwd:
            parser.error("new requires --cwd")
        if active.exists():
            print("BUSY: a turn is still running on the current thread; wait for it before switching.")
            return EXIT_BUSY
        # The thread itself is started together with the next turn: a thread
        # without a turn is never persisted and cannot be resumed later.
        (control / "cwd").write_text(args.cwd + "\n", encoding="utf-8")
        if current.exists():
            current.unlink()
        print("NEXT TURN STARTS A NEW THREAD in " + args.cwd)
        return 0

    if args.command in ("answer", "wait"):
        if not active.exists():
            print("IDLE: no turn is running.")
            return EXIT_IDLE
        if args.command == "answer" and not question.exists():
            print("NO QUESTION: Codex has no open question; use `steer` to add information to the running turn.")
            return EXIT_IDLE
        thread_id = current.read_text(encoding="utf-8").strip()
        turn_id = active.read_text(encoding="utf-8").strip()
        client = Client()
        buffered = []
        # Resuming subscribes this connection; the daemon re-sends a pending question.
        # Include turns so we immediately detect if the turn already finished while disconnected.
        res = client.request("thread/resume", {"threadId": thread_id, "excludeTurns": False}, on_event=buffered.append)
        answer = None
        if args.command == "answer":
            answer = (json.loads(question.read_text(encoding="utf-8")), Path(args.prompt_file).read_text(encoding="utf-8"))
        snap = find_turn(res, turn_id)
        if snap is not None and snap.get("status") != "inProgress":
            return finish_from_turn_snapshot(control, snap, answer=answer)
        return follow(control, client, thread_id, turn_id, answer, buffered=buffered)

    text = Path(args.prompt_file).read_text(encoding="utf-8")
    client = Client()

    if args.command == "steer":
        if not active.exists():
            print("IDLE: no turn is running; send it with `turn` instead.")
            return EXIT_IDLE
        thread_id = current.read_text(encoding="utf-8").strip()
        client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        result = client.request("turn/steer", {"threadId": thread_id, "expectedTurnId": active.read_text(encoding="utf-8").strip(),
                                               "input": text_input(text)})
        print("STEERED " + json.dumps(result, ensure_ascii=False))
        return 0

    if not args.mode:
        parser.error("turn requires --mode")
    if active.exists():
        print("BUSY: a turn is already running; use `steer` to add to it, or `wait`/`answer` to follow it.")
        return EXIT_BUSY
    if current.exists():
        thread_id = current.read_text(encoding="utf-8").strip()
        client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    else:
        cwd = (control / "cwd").read_text(encoding="utf-8").strip()
        config = {"model_reasoning_effort": EFFORT, "features.default_mode_request_user_input": True}
        thread_id = client.request("thread/start", {"cwd": cwd, "model": MODEL, "config": config})["thread"]["id"]
        current.write_text(thread_id + "\n", encoding="utf-8")
        with open(BASE / "threads.log", "a", encoding="utf-8") as log:
            log.write("{} {} {}\n".format(thread_id, args.lane, cwd))
    turn = client.request("turn/start", {"threadId": thread_id, "model": MODEL, "effort": EFFORT,
                                         "input": text_input(directive(args.mode, args.files) + "\n" + ASK_RULE + "\n" + text)})["turn"]
    active.write_text(turn["id"] + "\n", encoding="utf-8")
    (BASE / "latest_lane").write_text(args.lane + "\n", encoding="utf-8")
    print("TURN STARTED thread={} turn={} mode={}".format(thread_id, turn["id"], args.mode))
    return follow(control, client, thread_id, turn["id"])


if __name__ == "__main__":
    sys.exit(main())
