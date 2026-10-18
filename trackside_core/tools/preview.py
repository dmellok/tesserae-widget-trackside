"""Render one of the four Trackside widgets live, outside Tesserae: server fetch with
the real feeds, then client.js in Chromium.

    python trackside_core/tools/preview.py --widget lineup [--w 800 --h 480] [key=value ...]

Settings come from the environment: TRACKSIDE_CONTACT, FAL_KEY. The data dir
is tools/out/data. The page goes to tools/out/preview_<widget>.png."""

from __future__ import annotations

# The painter imports Tesserae's ``app.artwork`` (0.463+); outside the server,
# the repo beside this one (or TESSERAE_REPO) is put on the path.
import os as _os
import sys as _sys
from pathlib import Path as _Path

_TESSERAE = _Path(
    _os.environ.get("TESSERAE_REPO")
    or _Path(__file__).resolve().parents[3] / "tesserae"
)
if str(_TESSERAE) not in _sys.path:
    _sys.path.insert(0, str(_TESSERAE))

import argparse
import functools
import http.server
import importlib.util
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "trackside_core" / "tools" / "out"
PLACE = {
    "latitude": -28.0,
    "longitude": 153.43,
    "location": {"label": "Surfers Paradise", "timezone": "Australia/Brisbane"},
    "source": "goldcoast",
    "radius_km": "10",
}


def main() -> None:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--widget", default="lineup", choices=("photo", "plate", "lineup", "scene")
    )
    ap.add_argument("--w", type=int, default=800)
    ap.add_argument("--h", type=int, default=480)
    ap.add_argument("--tag", default="")
    ap.add_argument("pairs", nargs="*")
    a = ap.parse_args()
    options = dict(PLACE)
    for pair in a.pairs:
        k, _, v = pair.partition("=")
        options[k] = json.loads(v) if v[:1] in "[{" else v
    spec = importlib.util.spec_from_file_location(
        "trackside_widget", ROOT / f"trackside_{a.widget}" / "server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)  # type: ignore[union-attr]
    data_dir = OUT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "contact": os.environ.get("TRACKSIDE_CONTACT", ""),
        "fal_api_key": os.environ.get("FAL_KEY", ""),
        "vic_key": os.environ.get("VIC_KEY", ""),
    }
    data = server.fetch(
        options, settings, ctx={"data_dir": str(data_dir), "cell_w": a.w, "cell_h": a.h}
    )
    print(
        "state",
        data.get("state"),
        "error",
        data.get("error"),
        "vehicles",
        len(data.get("vehicles") or []),
        "status",
        data.get("status"),
        "notices",
        data.get("notices"),
    )
    # paintings run on threads of this process: wait for them, then fetch again so the page has them
    P = server._lib("paint")
    import time

    end = time.time() + 240
    while (lambda pr: pr["running"] or pr["queued"])(
        P.ART.progress(data_dir, {})
    ) and time.time() < end:
        time.sleep(1)
    if any("painting" in s for s in (data.get("status") or [])):
        data = server.fetch(
            options,
            settings,
            ctx={"data_dir": str(data_dir), "cell_w": a.w, "cell_h": a.h},
        )
        print(
            "again:",
            "vehicles",
            len(data.get("vehicles") or []),
            "status",
            data.get("status"),
            "notices",
            data.get("notices"),
        )
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(ROOT)
    )
    handler.log_message = lambda *x: None  # type: ignore[method-assign]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    (OUT / "preview_data.json").write_text(json.dumps(data), encoding="utf-8")
    html = f"""<!doctype html><html><body style="margin:0;background:#888"><div id="cell" style="width:{a.w}px;height:{a.h}px"></div>
    <script type="module">
      const m = await import('/trackside_core/static/trackside.js');
      const host = document.getElementById('cell');
      const shadow = host.attachShadow({{ mode: 'open' }});
      const data = await (await fetch('/trackside_core/tools/out/preview_data.json')).json();
      await m.render(shadow, {{ data, cell: {{ w: {a.w}, h: {a.h}, size: 'lg' }} }}, '{a.widget}');
      window.__done = true;
    </script></body></html>"""
    (OUT / "preview.html").write_text(html, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": a.w, "height": a.h})
        pg.on(
            "console",
            lambda msg: (
                print("  LOG", msg.type, msg.text[:200])
                if msg.type in ("error", "warning")
                else None
            ),
        )
        pg.on("pageerror", lambda e: print("  PAGEERROR", str(e)[:300]))
        pg.goto(f"http://127.0.0.1:{port}/trackside_core/tools/out/preview.html")
        pg.wait_for_function("window.__done === true", timeout=60000)
        pg.wait_for_timeout(400)
        out = OUT / f"preview_{a.tag or a.widget}.png"
        pg.screenshot(path=str(out))
        print("wrote", out)
        b.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
