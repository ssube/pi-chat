#!/usr/bin/env python3
"""Discover tmux panes, capture text, and insert a literal line."""
import argparse
import re
import subprocess
import sys
import unicodedata
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    socket = parser.add_mutually_exclusive_group()
    socket.add_argument("--socket-name", help="tmux -L server name")
    socket.add_argument("--socket-path", help="tmux -S socket path")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="list panes on the selected server")
    read = commands.add_parser("read", help="capture current pane text")
    read.add_argument("--target", required=True, help="exact pane ID, e.g. %%3")
    read.add_argument("--history", type=int, default=0, help="include this many scrollback lines")
    send = commands.add_parser("send", help="insert literal text, then Enter by default")
    send.add_argument("--target", required=True, help="exact pane ID, e.g. %%3")
    source = send.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--stdin", action="store_true", help="read exact text from stdin")
    send.add_argument("--no-enter", action="store_true", help="insert without submitting")
    args = parser.parse_args(argv)
    if args.action != "list" and not re.fullmatch(r"%[0-9]+", args.target):
        parser.error("--target must be an exact pane ID from list (e.g. %3)")
    if args.action == "read" and not 0 <= args.history <= 10000:
        parser.error("--history must be between 0 and 10000")
    text = None
    if args.action == "send":
        text = sys.stdin.read() if args.stdin else args.text
        if any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in text):
            parser.error("text must be a single line without control characters (no trailing newline)")

    base = ["tmux"]
    if args.socket_name is not None:
        base += ["-L", args.socket_name]
    if args.socket_path is not None:
        base += ["-S", args.socket_path]

    def run(*command, input=None):
        return subprocess.run(
            base + list(command), input=input, text=True,
            capture_output=True, check=True,
        ).stdout

    try:
        if args.action == "list":
            output = run("list-panes", "-a", "-F",
                         "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"
                         "\t#{pane_current_command}\tdead=#{pane_dead}\tmode=#{pane_in_mode}")
            sys.stdout.write(output)
        elif args.action == "read":
            alternate = run("display-message", "-p", "-t", args.target, "#{alternate_on}").strip()
            if alternate == "1" and args.history:
                parser.error("scrollback is unavailable on the alternate screen")
            # The default is the current screen; -a captures the saved screen.
            sys.stdout.write(run("capture-pane", "-p", "-t", args.target,
                                 "-S", str(-args.history)))
        else:
            state = run("display-message", "-p", "-t", args.target,
                        "#{pane_dead}:#{pane_in_mode}").strip()
            if state != "0:0":
                parser.error("pane is dead or in copy/other mode; no text sent")
            # Stdin avoids tmux command-separator parsing of literal semicolons.
            # A private named buffer avoids touching the user's clipboard/buffer.
            if text:
                buffer = "agent-tmux-" + uuid.uuid4().hex
                run("load-buffer", "-b", buffer, "-", input=text)
                try:
                    run("paste-buffer", "-r", "-b", buffer, "-t", args.target)
                finally:
                    # Delete once: on older tmux, deleting a missing named buffer
                    # can fall back to deleting the user's top buffer.
                    names = run("list-buffers", "-F", "#{buffer_name}").splitlines()
                    if buffer in names:
                        run("delete-buffer", "-b", buffer)
            if not args.no_enter:
                run("send-keys", "-t", args.target, "Enter")
            print("Inserted text" + (" without Enter." if args.no_enter else " and sent Enter.")
                  + " Read the pane to verify the result.")
        return 0
    except FileNotFoundError:
        print("tmux executable not found; install tmux in this environment.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        # Do not echo argv or text: terminal input may contain private data.
        print("tmux operation failed; check the selected server and pane. "
              "Input may be partially delivered; read before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
