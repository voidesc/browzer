# browzer

A real web page in a terminal. browzer runs the Chromium already on your system, without a
window, and draws its frames into a [kitty](https://sw.kovidgoyal.net/kitty/) pane with the
kitty graphics protocol; your keys and mouse go to the page. It is a thin layer in Python with
no dependencies beyond the standard library, so the browser you trust is the one your
distribution updates, and the part you read is small.

```
browzer https://example.com      # ctrl+l address, ctrl+t new tab, ctrl+w close, ctrl+q quit
browzer --ssh dev-box            # the page loads from the host's network: its localhost, its DNS
browzer ctl open https://...     # drive the running browzer from a shell or a script
browzer ctl snapshot             # the page as an outline with refs for `ctl click` and `ctl fill`
```

Needs Python 3.11, kitty, and Chromium (or Chrome) on `PATH`; `--help` lists the rest.
Tests run the real program and browser in a pseudo-terminal, no window:
`python3 -m unittest discover -s tests -t .`

MIT.
