"""browzer [URL] - a real Chromium browser in this terminal.

Keys: ctrl+l (or a click on the bar) edits the address: a URL, a host, or words to search for.
ctrl+t new tab, ctrl+w close tab, ctrl+pageup / ctrl+pagedown or alt+1..9 switch tabs.
ctrl+r or F5 reloads, alt+left / alt+right go back and forward, ctrl+c copies the selection
to the terminal's clipboard, ctrl+q quits. Everything else, and the mouse, goes to the page.

`browzer ctl COMMAND` drives a running browzer from a shell: see `browzer ctl --help`.
"""
import argparse
import os
import sys

from . import __version__, chromium
from .app import App, Unsupported
from .url import SEARCH, normalize


def xdg(var, default):
    return os.path.join(os.environ.get(var) or os.path.expanduser(default), "browzer")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="browzer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default="about:blank")
    ap.add_argument("--chromium", metavar="PATH", help="the browser to run (default: $BROWZER_CHROMIUM, else chromium on PATH)")
    ap.add_argument("--profile", metavar="DIR", help="the browser profile (default: browzer/profile under $XDG_DATA_HOME)")
    ap.add_argument("--temp-profile", action="store_true", help="a fresh profile, deleted on exit")
    ap.add_argument("--scale", type=float, default=float(os.environ.get("BROWZER_SCALE", 1)), metavar="N",
                    help="device pixels per CSS pixel (2 on a HiDPI screen; default 1 or $BROWZER_SCALE)")
    ap.add_argument("--transfer", choices=("auto", "file", "inline"), default=os.environ.get("BROWZER_TRANSFER", "auto"),
                    help="how frames reach the terminal: a file in shared memory (same machine) or inline (anywhere)")
    ap.add_argument("--search", metavar="URL", default=os.environ.get("BROWZER_SEARCH", SEARCH),
                    help="where words typed as an address go, {} being the words (default: %(default)s)")
    ap.add_argument("--stats", action="store_true", help="frame rate, frame size and draw time in the bar")
    ap.add_argument("--no-control", action="store_true", help="no control socket: `browzer ctl` cannot reach this one")
    ap.add_argument("--version", action="version", version=f"browzer {__version__}")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ctl"]:
        from . import ctl
        return ctl.main(argv[1:])
    args = ap.parse_args(argv)

    if not (os.isatty(0) and os.isatty(1)):
        print("browzer: needs a terminal on stdin and stdout", file=sys.stderr)
        return 2
    if args.scale <= 0:
        ap.error("--scale must be positive")
    state = xdg("XDG_STATE_HOME", "~/.local/state")
    os.makedirs(state, exist_ok=True)
    try:
        app = App(normalize(args.url, args.search), binary=chromium.find(args.chromium),
                  profile=None if args.temp_profile else (args.profile or os.path.join(xdg("XDG_DATA_HOME", "~/.local/share"), "profile")),
                  log_path=os.path.join(state, "chromium.log"), scale=args.scale, transfer=args.transfer, stats=args.stats,
                  search=args.search, control=not args.no_control)
        status = app.run()
    except (chromium.NotFound, Unsupported) as e:
        print(f"browzer: {e}", file=sys.stderr)
        return 1
    if app.error:
        print(f"browzer: {app.error}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
