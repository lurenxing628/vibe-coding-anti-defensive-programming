#!/usr/bin/env python3
"""Watch the Claude Code lanes driven by claude_drive.py in their live TUI.

The user starts this in an Antigravity terminal:

    claude watch          follow the lane whose turn started last (F3 cycles lanes)
    claude watch <lane>   stay on one lane; open several terminals to watch lanes side by side

It attaches to the tmux server hosting the lanes. When claude_drive.py switches a
lane to a new session it restarts Claude in the same window, so the view follows
by itself. Keyboard input to Claude is off by default (F2 toggles it), the mouse
wheel scrolls back, Ctrl-C quits the viewer and leaves the sessions running.
"""
import os
import subprocess
import sys
import time

from claude_drive import SERVER, SESSION, TMUX, Lane, session_exists, window_exists


def attach(lane):
    if lane is None:
        subprocess.run([TMUX, "-L", SERVER, "attach-session", "-t", SESSION])
        return
    # A grouped session shares the lanes' windows but keeps its own current window,
    # so the driver selecting windows for new turns does not move this view. It is
    # created attached: destroy-unattached would remove a detached one at once.
    view = "view-{}".format(os.getpid())
    subprocess.run([TMUX, "-L", SERVER, "new-session", "-t", SESSION, "-s", view,
                    ";", "set", "-t", view, "destroy-unattached", "on",
                    ";", "select-window", "-t", "{}:={}".format(view, lane)])


def main():
    lane = sys.argv[1] if len(sys.argv) > 1 else None
    waiting = False
    try:
        while True:
            if lane is None and session_exists() or lane is not None and window_exists(Lane(lane)):
                attach(lane)
                if session_exists():
                    return  # detached on purpose (Ctrl-C)
                print("\nClaude 会话所在的 tmux 已关闭，等待主代理重新开启……", flush=True)
                waiting = True
            elif not waiting:
                print("等待主代理开启 Claude {}……".format("会话" if lane is None else "车道 " + lane), flush=True)
                waiting = True
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
