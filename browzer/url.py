"""What was typed as an address: a URL, a file, a host, or words to search for."""
import os
import re
from urllib.parse import quote, quote_plus

SEARCH = "https://duckduckgo.com/?q={}"
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_OPAQUE = ("about:", "data:", "file:", "view-source:", "chrome:", "javascript:")
_HOST = re.compile(r"^(\[[0-9a-fA-F:]+\]|[\w-]+(\.[\w-]+)*)(:\d+)?([/?#].*)?$")
_PLAIN_HTTP = re.compile(r"^(localhost|127\.|10\.|192\.168\.|\[::1\]|[\w-]+:\d+)")


def normalize(text, search=SEARCH, bare_file=True):
    """The URL to load for `text`. `bare_file`: a single word that names a file here is that
    file (right on a command line, wrong for something typed into the address bar)."""
    text = text.strip()
    if not text:
        return "about:blank"
    if _SCHEME.match(text) or text.startswith(_OPAQUE):
        return text
    pathlike = text.startswith(("/", "~", "./", "../")) or bare_file
    path = os.path.abspath(os.path.expanduser(text))
    if pathlike and os.path.exists(path):
        return "file://" + quote(path)
    m = _HOST.match(text)
    if m and " " not in text:
        host = m[1]
        if "." in host or host == "localhost" or host.startswith("[") or m[3]:
            return ("http://" if _PLAIN_HTTP.match(text) else "https://") + text
    return search.format(quote_plus(text))
