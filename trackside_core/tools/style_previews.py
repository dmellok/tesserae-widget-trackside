"""Paint the style picker's sample pictures: one tram, from one openly
licensed Wikimedia Commons photograph, in every style, through fal.ai, as
small JPEGs in trackside_core/static/styles/ that the widgets' Style options
point at (``choices[].image``). About US$0.04 a style.

    FAL_KEY=... python3 trackside_core/tools/style_previews.py            # the missing ones
    python3 trackside_core/tools/style_previews.py --key-file ~/fal_key --only poster,deco --force

The reference and its credit are in REFERENCE below and in ATTRIBUTION.md;
the full-size paintings go to trackside_core/tools/out/styles/ (not shipped).
Needs a Tesserae checkout beside this repo (or TESSERAE_REPO) for app.fal."""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_TESSERAE = Path(os.environ.get("TESSERAE_REPO") or ROOT.parent / "tesserae")
if str(_TESSERAE) not in sys.path:
    sys.path.insert(0, str(_TESSERAE))
from app import fal  # noqa: E402

OUT = ROOT / "trackside_core" / "static" / "styles"
FULL = ROOT / "trackside_core" / "tools" / "out" / "styles"
THUMB = (320, 240)
LARGE = (800, 600)  # the picker's enlarged view, in static/styles/large/
MARGIN = 1.2  # the sample's room round the vehicle, as a share of its trimmed size
BUSY = 0.02  # a column or row is the vehicle's when this share of it differs from the paper
REFERENCE = {
    "num": "6089",
    "typ": "E-class tram",
    "op": "Yarra Trams",
    "cls": "tram",
    "len": 33.4,
    "src": "https://upload.wikimedia.org/wikipedia/commons/7/7f/Yarra_Trams_E-class_6089_Route_30%2C_January_2022_1.jpg?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original",
    "by": "Marcnut1996",
    "lic": "CC BY-SA 4.0",
    "link": "https://commons.wikimedia.org/wiki/File:Yarra_Trams_E-class_6089_Route_30,_January_2022_1.jpg",
}
UA = "TesseraeStylePreviews/1.0 (https://github.com/dmellok/tesserae-widget-trackside)"


def _paint_module():
    spec = importlib.util.spec_from_file_location(
        "trackside_core_server", ROOT / "trackside_core" / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.lib("paint")


def _reference() -> bytes:
    cache = FULL / "reference.jpg"
    if cache.exists():
        return cache.read_bytes()
    req = urllib.request.Request(REFERENCE["src"], headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=30).read()
    FULL.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw)
    return raw


def _busy_box(mask, w: int, h: int) -> tuple[int, int, int, int] | None:
    px = mask.load()
    cols = [sum(1 for y in range(0, h, 2) if px[x, y]) * 2 for x in range(w)]
    rows = [sum(1 for x in range(0, w, 2) if px[x, y]) * 2 for y in range(h)]
    # a line the page's whole length (a drawn frame) is not the vehicle
    xs = [x for x, n in enumerate(cols) if h * BUSY < n < h * 0.85]
    ys = [y for y, n in enumerate(rows) if w * BUSY < n < w * 0.85]
    if not xs or not ys:
        return mask.getbbox()
    return xs[0], ys[0], xs[-1] + 1, ys[-1] + 1


def thumbnail(raw: bytes, size: tuple[int, int] = THUMB) -> bytes:
    from PIL import Image, ImageChops, ImageOps

    im = Image.open(io.BytesIO(raw)).convert("RGB")
    # close in on the vehicle: the paper is what the corners show, and the
    # sample is cut to what differs from it, with a margin, before the fit
    w, h = im.size
    corners = [
        im.getpixel(xy) for xy in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))
    ]
    paper = tuple(sorted(c[i] for c in corners)[1] for i in range(3))
    diff = (
        ImageChops.difference(im, Image.new("RGB", im.size, paper))
        .convert("L")
        .point(lambda v: 255 if v > 28 else 0)
    )
    # a tram or train is long and runs on longer rails under a longer wire:
    # the cut is to the columns and rows with more marked than a line or two
    box = _busy_box(diff, w, h)
    if box and (box[2] - box[0]) < w * 0.97:
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        # widen to the sample's shape, with a margin round it
        tw = max(bw * MARGIN, bh * MARGIN * size[0] / size[1])
        th = tw * size[1] / size[0]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        crop = (
            int(cx - tw / 2),
            int(cy - th / 2),
            int(cx + tw / 2),
            int(cy + th / 2),
        )
        bg = Image.new("RGB", (crop[2] - crop[0], crop[3] - crop[1]), paper)
        bg.paste(
            im.crop(
                (max(0, crop[0]), max(0, crop[1]), min(w, crop[2]), min(h, crop[3]))
            ),
            (max(0, -crop[0]), max(0, -crop[1])),
        )
        im = bg
    im = ImageOps.fit(im, size, method=Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80, optimize=True, progressive=True)
    return out.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key-file", default="")
    ap.add_argument("--only", default="", help="comma-separated style ids")
    ap.add_argument(
        "--force", action="store_true", help="paint again even when the sample exists"
    )
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument(
        "--rethumb",
        action="store_true",
        help="remake the samples from the kept paintings, nothing painted",
    )
    a = ap.parse_args()
    if a.rethumb:
        OUT.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(FULL.glob("*.jpg")):
            if f.stem != "reference" and (not a.only or f.stem in a.only.split(",")):
                (OUT / f.name).write_bytes(thumbnail(f.read_bytes()))
                (OUT / "large").mkdir(exist_ok=True)
                (OUT / "large" / f.name).write_bytes(thumbnail(f.read_bytes(), LARGE))
                n += 1
        print(f"{n} samples made again from the kept paintings")
        return 0
    key = (
        Path(a.key_file).expanduser().read_text(encoding="utf-8").strip()
        if a.key_file
        else ""
    ) or os.environ.get("FAL_KEY", "").strip()
    if not key:
        print("no fal.ai key: --key-file or FAL_KEY", file=sys.stderr)
        return 2
    P = _paint_module()
    styles = [s for s in P.STYLES if s not in P.HIDDEN and (not a.only or s in a.only.split(","))]
    todo = [s for s in styles if a.force or not (OUT / f"{s}.jpg").exists()]
    ref = fal.data_uri(_reference())
    OUT.mkdir(parents=True, exist_ok=True)
    FULL.mkdir(parents=True, exist_ok=True)
    client = fal.FalClient(key)

    def one(style: str) -> str:
        # the line-up's payload for an exact painting of one vehicle from her own photograph
        job = {
            "num": REFERENCE["num"],
            "typ": REFERENCE["typ"],
            "op": REFERENCE["op"],
            "cls": REFERENCE["cls"],
            "len": REFERENCE["len"],
            "car": "",
            "exact": True,
            "style": style,
            "how": "",
            "backdrop": "none",
            "weather_words": "",
            "view": "side",
            "fidelity": "photo",
            "model": P.model_for(style, P.DEFAULT_MODEL),
            "photo": ref,
            "by": REFERENCE["by"],
            "lic": REFERENCE["lic"],
            "link": REFERENCE["link"],
            "ua": UA,
            "aspect": "4:3",
            "side": "",
            "side_by": "",
            "side_lic": "",
        }
        try:
            raw = client.generate(
                job["model"], P.prompt_for(job), refs=[ref], aspect="4:3", retries=1
            )
        except Exception as exc:  # noqa: BLE001 - one style failing leaves the rest
            return f"{style}: failed ({exc})"
        (FULL / f"{style}.jpg").write_bytes(raw)
        (OUT / f"{style}.jpg").write_bytes(thumbnail(raw))
        (OUT / "large").mkdir(exist_ok=True)
        (OUT / "large" / f"{style}.jpg").write_bytes(thumbnail(raw, LARGE))
        return f"{style}: ok"

    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)
    print(
        f"{len(todo)} asked, about US${client.spent_cents / 100:.2f} spent", flush=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
