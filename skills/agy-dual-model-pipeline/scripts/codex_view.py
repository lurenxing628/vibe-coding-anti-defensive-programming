#!/usr/bin/env python3
"""Watch the Codex thread driven by codex_drive.py in a live Codex TUI.

The user starts this once in an Antigravity terminal (a real terminal, so the
TUI renders properly):

    codex watch          follow the lane whose turn started last
    codex watch <lane>   stay on one lane; open several terminals to watch lanes side by side

It attaches `codex resume <thread> --no-alt-screen` to the shared app-server daemon and, when
the followed lane switches threads (or another lane starts a turn), restarts the TUI on the
new one. Ctrl-C quits.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODEX = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
BASE = Path.home() / ".cache" / "agy-dual-model-pipeline" / "codex"  # see codex_drive.py
SESSIONS = Path.home() / ".codex" / "sessions"


def persisted(thread):
    # A thread becomes resumable once its first turn writes the rollout file.
    return any(SESSIONS.glob("*/*/*/rollout-*-{}.jsonl".format(thread)))


def stop(child):
    child.terminate()
    child.wait()
    # The TUI was killed mid-draw: show the cursor again and restore line mode.
    print("\033[?25h", end="", flush=True)
    subprocess.run(["stty", "sane"])


def read(path):
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def main():
    pinned = sys.argv[1] if len(sys.argv) > 1 else None
    shown, child = None, None
    print("等待主代理开启 Codex {}……".format("会话" if pinned is None else "车道 " + pinned))
    try:
        while True:
            lane = pinned or read(BASE / "latest_lane")
            thread = read(BASE / lane / "current_thread") if lane else None
            if thread != shown and (thread is None or persisted(thread)):
                if child and child.poll() is None:
                    stop(child)
                shown = thread
                if thread:
                    print("\n===== Codex 车道 {} 会话 {} =====".format(lane, thread), flush=True)
                    # Inline mode keeps the transcript in the terminal's own
                    # scrollback, so it can be scrolled back across turns and threads.
                    child = subprocess.Popen([CODEX, "resume", thread, "--remote", "unix://", "--no-alt-screen"])
                else:
                    child = None
                    print("\n当前 Codex 会话已结束，等待主代理开启新会话……")
            time.sleep(2)
    except KeyboardInterrupt:
        if child and child.poll() is None:
            stop(child)


if __name__ == "__main__":
    main()
