"""Regenerate drawio_icons.json from draw.io's own shape libraries.

Needs a draw.io webapp checkout and Playwright (Chromium):
    git clone --depth 1 --filter=blob:none --sparse https://github.com/jgraph/drawio.git /tmp/drawio
    git -C /tmp/drawio sparse-checkout set src/main/webapp/js src/main/webapp/img/lib/azure2
    pip install playwright && playwright install chromium
    python scripts/dump_drawio_icons.py /tmp/drawio/src/main/webapp
The page loads viewer-static.min.js + stencils.min.js offline and lists every registered stencil.
"""
import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script>window.STENCIL_PATH='stencils';window.SHAPES_PATH='shapes';</script>
<script src="js/viewer-static.min.js"></script><script src="js/stencils.min.js"></script></head><body></body></html>"""


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main(webapp: Path, out: Path):
    (webapp / "_icons.html").write_text(PAGE)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(webapp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{srv.server_port}/_icons.html")
            names, shapes = page.evaluate("""() => {
                mxStencilRegistry.getStencil('mxgraph.aws4.ecr'); mxStencilRegistry.getStencil('mxgraph.gcp2.bigquery');
                return [Object.keys(mxStencilRegistry.stencils),
                        Object.keys(mxCellRenderer.defaultShapes).filter(k => /^mxgraph\\.(aws4|gcp2)\\./.test(k))]; }""")
            browser.close()
    finally:
        srv.shutdown()
        (webapp / "_icons.html").unlink()
    azure = webapp / "img/lib/azure2"
    data = {
        "_source": "draw.io (jgraph/drawio) stencil registry + img/lib/azure2; regenerate with scripts/dump_drawio_icons.py",
        "aws4": sorted(n.split(".", 2)[2] for n in names if n.startswith("mxgraph.aws4.")),
        "gcp2": sorted(n.split(".", 2)[2] for n in names if n.startswith("mxgraph.gcp2.")),
        "azure2": sorted(str(p.relative_to(azure)) for p in azure.rglob("*.svg")),
        "shapes": sorted(shapes),
    }
    out.write_text(json.dumps(data, separators=(",", ":")))
    print({k: len(v) for k, v in data.items() if k != "_source"})


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(__file__).resolve().parents[1] / "drawio_icons.json")
