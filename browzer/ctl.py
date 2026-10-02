"""browzer ctl COMMAND - drive a running browzer from a shell.

  ls                       the running browzers and their tabs
  open URL [--new-tab]     load a URL, a file, or words to search for; waits for the page
  text                     the page as plain text
  snapshot                 the page as an outline; things you can use carry a ref like [e12]
  click REF | click X Y    click what a ref names, or a point of the page (CSS pixels)
  fill REF TEXT            put TEXT into a field, replacing what is there
  type TEXT                type into whatever has the focus
  press KEY                Enter, Tab, Escape, ArrowDown, PageDown, ctrl+a ...
  eval EXPRESSION          run JavaScript in the page, print its value as JSON
  screenshot [FILE]        save what the page shows as a PNG, print the path
  back | forward | reload
  tab N | close            show tab N; close the tab
  quit

--tab N acts on tab N (and shows it); --instance PID picks a browzer when several run
(default: $BROWZER_INSTANCE, else the one started last).
"""
import argparse
import base64
import json
import os
import socket
import sys
import tempfile

from . import control
from .url import SEARCH, normalize


def request(path, msg, timeout=control.DEFAULT_TIMEOUT):
    """One command to one browzer; its answer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout + 5)
        s.connect(path)
        s.sendall(json.dumps({**msg, "timeout": timeout}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            data += chunk
    if not data:
        return {"ok": False, "error": "browzer closed the connection without an answer"}
    return json.loads(data)


def show_tabs(tabs, out):
    for t in tabs:
        mark = "*" if t["active"] else " "
        print(f"  {mark} {t['index']}  {t['title'] or '(no title)'}  {t['url']}{'  (loading)' if t['loading'] else ''}", file=out)


def main(argv=None, out=sys.stdout, err=sys.stderr):
    ap = argparse.ArgumentParser(prog="browzer ctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", metavar="PID", type=int, default=int(os.environ.get("BROWZER_INSTANCE") or 0) or None)
    ap.add_argument("--tab", metavar="N", type=int)
    ap.add_argument("--timeout", metavar="SECONDS", type=float, default=control.DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="print the answer as browzer sent it")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("ls")
    p = sub.add_parser("open")
    p.add_argument("url")
    p.add_argument("--new-tab", action="store_true")
    for name in ("text", "snapshot", "back", "forward", "reload", "close", "quit"):
        sub.add_parser(name)
    sub.add_parser("click").add_argument("target", nargs="+", metavar="REF | X Y")
    p = sub.add_parser("fill")
    p.add_argument("ref")
    p.add_argument("text")
    sub.add_parser("type").add_argument("text")
    sub.add_parser("press").add_argument("key")
    sub.add_parser("eval").add_argument("expression")
    sub.add_parser("screenshot").add_argument("file", nargs="?")
    sub.add_parser("tab").add_argument("n", type=int)
    args = ap.parse_args(argv)

    try:
        running = control.instances()
    except OSError as e:
        print(f"browzer ctl: {e}", file=err)
        return 1
    if args.cmd == "ls":
        answers = []
        for pid, path in running:
            try:
                answers.append(request(path, {"cmd": "ls"}, 5))
            except (OSError, ValueError):
                continue
        if args.json:
            print(json.dumps(answers), file=out)
        for a in answers:
            if a.get("ok") and not args.json:
                print(f"browzer {a['pid']}" + (f"  via ssh {a['ssh']}" if a.get("ssh") else ""), file=out)
                show_tabs(a["tabs"], out)
        if not answers and not args.json:
            print("no browzer is running", file=out)
        return 0

    chosen = [(pid, path) for pid, path in running if args.instance in (None, pid)]
    if not chosen:
        which = f"browzer {args.instance} is not running" if args.instance else "no browzer is running"
        print(f"browzer ctl: {which} (start one in a terminal: browzer [URL])", file=err)
        return 3
    msg = {"cmd": args.cmd}
    if args.tab is not None:
        msg["tab"] = args.tab
    if args.cmd == "open":
        msg.update(url=normalize(args.url, os.environ.get("BROWZER_SEARCH", SEARCH)), new_tab=args.new_tab)
    elif args.cmd == "click":
        if len(args.target) == 1:
            msg["ref"] = args.target[0]
        elif len(args.target) == 2:
            msg.update(x=float(args.target[0]), y=float(args.target[1]))
        else:
            ap.error("click takes a ref, or X and Y")
    elif args.cmd == "fill":
        msg.update(ref=args.ref, text=args.text)
    elif args.cmd in ("type",):
        msg["text"] = args.text
    elif args.cmd == "press":
        msg["key"] = args.key
    elif args.cmd == "eval":
        msg["expression"] = args.expression
    elif args.cmd == "tab":
        msg["tab"] = args.n

    try:
        answer = request(chosen[0][1], msg, args.timeout)
    except (OSError, ValueError) as e:
        print(f"browzer ctl: browzer {chosen[0][0]} did not answer ({e})", file=err)
        return 1
    if not answer.get("ok"):
        print(f"browzer ctl: {answer.get('error', 'failed')}", file=err)
        return 1
    if args.cmd == "screenshot":
        path = args.file or tempfile.mkstemp(prefix="browzer-shot-", suffix=".png")[1]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(answer.pop("png", "")))
        answer["file"] = os.path.abspath(path)
    if args.json:
        print(json.dumps(answer), file=out)
    elif args.cmd == "screenshot":
        print(answer["file"], file=out)
    elif args.cmd in ("text", "snapshot"):
        print(answer["text"], file=out)
    elif args.cmd == "eval":
        if answer.get("value") is not None:
            print(json.dumps(answer["value"]), file=out)
        else:   # undefined, null, or something JSON cannot carry (a DOM node, a function)
            print("undefined" if answer.get("type") == "undefined" else answer.get("description") or "null", file=out)
    elif "tabs" in answer:
        show_tabs(answer["tabs"], out)
    elif "url" in answer:
        print(f"{answer.get('title') or '(no title)'}\n{answer['url']}{'  (still loading)' if answer.get('loading') else ''}", file=out)
    return 0
