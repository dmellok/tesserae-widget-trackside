"""One fetch for the four Trackside widgets: the trains and trams near a
place, their class, line and photograph, their paintings, and the words for
the page. Loaded by path from each widget's server.py."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
logger = logging.getLogger("app.plugins.trackside")


def _sibling(name: str) -> Any:
    full = f"_tesserae_plugins.trackside_core.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


N = _sibling("net")
R = _sibling("rt")
P = _sibling("paint")
W = _sibling("watch")
SG = _sibling("sightings")
# the ledger of passes: one id per airframe (vehicle), the same as the first part of its painting's key
SIGHT_ID = lambda r: f"{r.get('op') or ''} {r.get('num') or ''}".strip()
SIGHT_NAME = lambda r: " · ".join(
    x for x in (r.get("typ") or "", r.get("line") or "") if x
)

WIDGETS = ("photo", "plate", "lineup", "scene")
RADII = ("1", "2", "3", "5", "10", "15", "25")
ORDERS = ("nearest", "fastest", "longest")
MODES = ("tram", "train", "regional", "subway")
PICTURES = ("photos", "paintings")
PANELS = ("colour", "gray16", "gray4", "mono")
PAPERS = ("cream", "white", "aged", "blueprint", "sky")
LABELS = (
    "type",
    "line",
    "dest",
    "number",
    "operator",
    "status",
    "speed",
    "distance",
    "heading",
    "seen",
)
UNPAINTED = ("silhouette", "photo", "skip")
PAINTS = ("all", "own")
PERIODS = ("now", "since")
WATCH_S = 60  # the feeds are asked this often while a page watches (Transport Victoria allows 24 a minute)
GONE_S = 120  # a vehicle not reported for this long has passed
MAX_COUNT = 12
POOL = 6
PICTURE_PX = 700
HERO_PX = 1400
PHOTO_PX = 1600
BUDGET_S = 4.2  # the composer gives a widget six seconds; the lookups stop asking here
LIVE_S = 30
FONTS = ("fell", "archivo", "georgia", "page")
LOOKS = ("plate", "pass", "tiles", "poster", "split")
# the options that choose how the cell looks, not which vehicles it shows
LOOK_OPTIONS = frozenset(
    (
        "font",
        "scale",
        "fidelity",
        "pictures",
        "style",
        "custom_style",
        "build_medium",
        "build_period",
        "build_palette",
        "build_line",
        "build_texture",
        "build_mood",
        "build_extra",
        "model",
        "backdrop",
        "view",
        "unpainted",
        "paint",
        "labels",
        "text_size",
        "paper",
        "panel",
        "header",
        "footer",
        "title",
        "strip",
        "photo_fit",
        "colours",
        "look",
    )
)
# the operators' colours for the modern looks: main, second, a tint, a shade, a third
BRANDS: dict[str, dict[str, str]] = {
    "Yarra Trams": {
        "primary": "#2f7d3a",
        "secondary": "#9fd08a",
        "tint": "#e8f3e2",
        "shade": "#163d1c",
        "third": "#64a85a",
        "ink": "#ffffff",
        "ink2": "#10240f",
    },
    "Metro Trains Melbourne": {
        "primary": "#0b5fa5",
        "secondary": "#55b3e8",
        "tint": "#e4f1fa",
        "shade": "#072f52",
        "third": "#3d8fd0",
        "ink": "#ffffff",
        "ink2": "#06223b",
    },
    "V/Line": {
        "primary": "#7d1f80",
        "secondary": "#d39bd6",
        "tint": "#f4e7f5",
        "shade": "#3a0c3c",
        "third": "#a957ad",
        "ink": "#ffffff",
        "ink2": "#2b072d",
    },
    "MBTA": {
        "primary": "#1c2a39",
        "secondary": "#ed8b00",
        "tint": "#eceff2",
        "shade": "#0b131b",
        "third": "#00843d",
        "ink": "#ffffff",
        "ink2": "#111111",
    },
}


def _choice(value: Any, choices: tuple[str, ...], default: str = "") -> str:
    v = str(value or "").strip()
    return v if v in choices else (default if default in choices else choices[0])


def _int(value: Any, default: int, low: int, high: int) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return max(low, min(high, int(float(str(value).strip()))))
    return default


def _tz(options: dict[str, Any]) -> ZoneInfo | None:
    loc = options.get("location")
    name = loc.get("timezone") if isinstance(loc, dict) else ""
    with contextlib.suppress(Exception):
        if name:
            return ZoneInfo(str(name))
    return None


def place_of(
    options: dict[str, Any], ctx: dict[str, Any]
) -> tuple[float, float, str] | None:
    loc = options.get("location")
    label = str(loc.get("label") or "") if isinstance(loc, dict) else ""
    for lat, lon in (
        (options.get("latitude"), options.get("longitude")),
        (ctx.get("home_lat"), ctx.get("home_lon")),
    ):
        with contextlib.suppress(TypeError, ValueError):
            la, lo = float(lat), float(lon)
            if la or lo:
                return la, lo, label
    return None


def compass(deg: int) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(deg / 45) % 8]


# ----------------------------------------------------------------------
# Words
# ----------------------------------------------------------------------
def distance_of(r: dict[str, Any]) -> str:
    return "under 100 m" if r["km"] < 0.1 else f"{r['km']:g} km {compass(r['dir'])}"


def where_of(r: dict[str, Any]) -> str:
    if r.get("closest_text"):
        return f"closest {distance_of(r)} at {r['closest_text']}"
    return distance_of(r)


def seen_of(r: dict[str, Any]) -> str:
    if r.get("seen_text"):
        return r["seen_text"]
    age = int(r.get("age_s") or 0)
    return "reported just now" if age < 90 else f"reported {age // 60} min ago"


def speed_of(r: dict[str, Any]) -> str:
    if r.get("kmh"):
        return f"{r['kmh']:g} km/h"
    return "stopped" if r.get("status") == "at a stop" else ""


def heading_of(r: dict[str, Any]) -> str:
    return f"heading {compass(r['trk'])}" if r.get("trk", -1) >= 0 else ""


def lines_for(r: dict[str, Any], labels: list[str]) -> list[str]:
    out: list[str] = []
    for lab in labels:
        if lab == "type":
            out.append(r["typ"])
        elif lab == "line" and r.get("line"):
            out.append(r["line"])
        elif lab == "dest" and r.get("dest"):
            out.append(f"to {r['dest']}")
        elif lab == "number" and r.get("num"):
            out.append(f"No. {r['num']}")
        elif lab == "operator" and r.get("op"):
            out.append(r["op"])
        elif lab == "status" and r.get("status"):
            out.append(r["status"])
        elif lab == "speed":
            out.append(speed_of(r))
        elif lab == "distance":
            out.append(where_of(r))
        elif lab == "heading":
            out.append(heading_of(r))
        elif lab == "seen":
            out.append(seen_of(r))
    return [x for x in out if x]


def about_of(r: dict[str, Any]) -> str:
    who = r.get("about") or ("a tram" if r["cls"] == "tram" else "a train")
    run = f" on {r['line']}" if r.get("line") else ""
    run += f", bound for {r['dest']}" if r.get("dest") else ""
    doing = {
        "at a stop": "standing at a stop",
        "approaching a stop": "drawing in to a stop",
        "between stops": "running between stops",
    }.get(
        r.get("status") or "",
        r["status"]
        if str(r.get("status") or "").startswith(("due at", "at "))
        else "running",
    )
    speed = f" at {r['kmh']:g} km/h" if r.get("kmh") else ""
    op = f", run by {r['op']}" if r.get("op") else ""
    where = (
        "under 100 m away"
        if r["km"] < 0.1
        else f"{r['km']:g} km to the {compass(r['dir'])}"
    )
    return f"No. {r['num']} is {who}{op}. She is {doing}{run}{speed}, {where}."


def subject_id(options: dict[str, Any]) -> str:
    return hashlib.sha1(
        json.dumps(
            {k: v for k, v in sorted(options.items()) if k not in LOOK_OPTIONS},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:16]


def watched(
    options: dict[str, Any],
    source: str,
    key: str,
    place: tuple[Any, ...],
    radius: float,
    data_dir: Path,
    ua: str,
    rows: list[dict[str, Any]],
    now: datetime,
    preview: bool,
    base: dict[str, Any],
    tz: Any,
) -> list[dict[str, Any]]:
    """Everything seen since this cell's last render, each at her closest
    approach, with when that was and whether she is still within reach."""
    lat, lon = place[0], place[1]
    feeds, _header, _op = R.feeds_for(source, options)
    wkey = f"trackside|{source}|{'|'.join(u for u, _m in feeds)}|{lat:.3f}|{lon:.3f}|{radius:g}"

    def poll() -> list[dict[str, Any]]:
        got, _err, _feed = R.nearby(
            source, options, key, lat, lon, radius, data_dir, ua
        )
        SG.ledger(data_dir).note(got, SIGHT_ID, SIGHT_NAME)
        return got

    w = W.watch_for(
        wkey, poll, WATCH_S, data_dir, lambda r: str(r.get("vid") or r.get("num") or "")
    )
    ts = now.timestamp()
    w.note(rows, ts)
    t0 = W.window(data_dir, subject_id(options), ts, advance=not preview)
    out = w.since(t0, ts)
    for r in out:
        here = ts - r["seen_last"] < GONE_S
        r["closest_text"] = W.clock(r["closest_at"], tz)
        r["seen_text"] = (
            f"within reach since {W.clock(r['seen_first'], tz)}"
            if here
            else f"passed {W.clock(r['seen_first'], tz)} to {W.clock(r['seen_last'], tz)}"
        )
        r["present"] = here
    since = W.clock(t0, tz)
    base["period"] = f"since {since}"
    base["since"] = t0
    base["watch"] = {
        "since": since,
        "seen": len(out),
        "polls": w.polls,
        "error": w.error,
        "present": sum(1 for r in out if r["present"]),
    }
    return out


# ----------------------------------------------------------------------
# The fetch
# ----------------------------------------------------------------------
def fetch(
    options: dict[str, Any], settings: dict[str, Any], ctx: dict[str, Any], widget: str
) -> dict[str, Any]:
    widget = widget if widget in WIDGETS else "lineup"
    fetch_started = time.time()
    settings = settings or {}
    data_dir = Path(ctx.get("data_dir") or ".")
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)
    preview = bool(ctx.get("preview"))
    ua = R.user_agent(str(settings.get("contact") or ""))
    art = P.ART.open(ctx, settings, subject=subject_id(options))
    fal_key = art.key

    source = _choice(options.get("source"), R.SOURCES, "melbourne")
    # Melbourne takes whichever key there is: the PTV Timetable API's when only that one is given
    if (
        source == "melbourne"
        and not str(settings.get("vic_key") or "").strip()
        and str(settings.get("ptv_devid") or "").strip()
        and str(settings.get("ptv_key") or "").strip()
    ):
        source = "ptv"
    key = (
        f"{str(settings.get('ptv_devid') or '').strip()}:{str(settings.get('ptv_key') or '').strip()}"
        if source == "ptv"
        else str(settings.get("vic_key") or "").strip()
        if source == "melbourne"
        else str(options.get("feed_key") or "").strip()
    )
    radius = float(_choice(options.get("radius_km"), RADII, "3"))
    order = _choice(options.get("order"), ORDERS)
    count = _int(options.get("count"), 6, 1, MAX_COUNT)
    if widget in ("photo", "plate", "scene"):
        count = 1 + (_int(options.get("strip"), 4, 0, 8) if widget == "photo" else 0)
    modes = (
        [m for m in options.get("modes") or MODES if m in MODES]
        if isinstance(options.get("modes"), list)
        else list(MODES)
    )
    pictures = (
        _choice(options.get("pictures"), PICTURES) if widget != "photo" else "photos"
    )
    panel = _choice(options.get("panel"), PANELS)
    paper = _choice(options.get("paper"), PAPERS)
    default_labels = ["type", "line", "distance"]
    labels = (
        [x for x in options.get("labels") if x in LABELS]
        if isinstance(options.get("labels"), list)
        else default_labels
    )
    colours = (
        _choice(options.get("colours"), ("operator", "paper"), "operator")
        if widget == "plate"
        else "paper"
    )
    look = (
        _choice(options.get("look"), LOOKS, "plate") if widget == "plate" else "plate"
    )
    header = _choice(options.get("header"), ("top", "bottom", "hidden"))
    footer = _choice(options.get("footer"), ("show", "credits", "hide"))
    text_size = _int(options.get("text_size"), 100, 60, 150)
    style = str(options.get("style") or "poster")
    how = ""
    if style == "builder":
        how = P.build_how(options)
        style = P.register_custom(how)
    elif style == "custom":
        how = (
            " ".join(str(options.get("custom_style") or "").split())[:600]
            or P.CUSTOM_EXAMPLE
        )
        style = P.register_custom(how)
    elif style not in P.STYLES:
        style = "poster"
    model = P.model_of(str(options.get("model") or P.DEFAULT_MODEL))
    backdrop = _choice(options.get("backdrop"), tuple(P.BACKDROPS))
    if widget == "lineup":
        backdrop = "none"
    if widget == "scene" and backdrop == "none":
        backdrop = "street"
    view = _choice(options.get("view"), tuple(P.VIEWS))
    fidelity = _choice(
        options.get("fidelity"), P.FIDELITIES, "side" if widget == "lineup" else "photo"
    )
    own_sides = R.own_side_views(settings.get("side_views"))
    paint = _choice(options.get("paint"), PAINTS, "all")
    weather = weather_text = ""

    tz = _tz(options)
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    place = place_of(options, ctx)
    title = " ".join(str(options.get("title") or "").split()) or (
        place[2] if place and place[2] else "Trackside"
    )
    base: dict[str, Any] = {
        "widget": widget,
        "state": "ok",
        "title": title,
        "area": f"within {radius:g} km",
        "period": "on the rails now",
        "date": now.strftime("%a %-d %b")
        if sys.platform != "win32"
        else now.strftime("%a %d %b"),
        "time": now.strftime("%H:%M"),
        "vehicles": [],
        "within": 0,
        "shown": 0,
        "notices": [],
        "status": [],
        "credits": [],
        "live_refresh_s": 0,
        "header": header,
        "footer": footer,
        "paper": paper,
        "panel": panel,
        "text_size": text_size,
        "labels": labels,
        "colours": colours,
        "look": look,
        "pictures": pictures,
        "style": style,
        "style_label": P.style_of(style)[0],
        "backdrop": backdrop,
        "photo_fit": _choice(options.get("photo_fit"), ("cover", "contain")),
        "font_css": N.font_css(("im_fell", "im_fell_sc", "archivo_narrow")),
        "font": _choice(options.get("font"), FONTS),
        "scale": _int(options.get("scale"), 100, 40, 100),
    }
    if not place:
        return {
            **base,
            "state": "error",
            "error": "Set a place: the cell's location, or the app's location in Settings.",
        }
    if backdrop == "live" and pictures == "paintings":
        tod, wx = R.weather_now(
            place[0], place[1], now.hour * 60 + now.minute, data_dir, ua
        )
        weather, weather_text = f"{tod}_{wx}", R.weather_words(tod, wx)

    rows, err, feed = R.nearby(
        source, options, key, place[0], place[1], radius, data_dir, ua
    )
    if err and not rows and not feed:
        return {**base, "state": "error", "error": err}
    SG.ledger(data_dir).note(rows, SIGHT_ID, SIGHT_NAME)
    if _choice(options.get("period"), PERIODS, "now") == "since":
        rows = watched(
            options,
            source,
            key,
            place,
            radius,
            data_dir,
            ua,
            rows,
            now,
            preview,
            base,
            tz,
        )
    rows = [
        r
        for r in rows
        if r.get("mode") in modes or (r.get("mode") not in MODES and r["cls"] in modes)
    ]
    base["within"] = len(rows)
    if base.get("watch"):  # what the page shows, not every mode the watch heard
        base["watch"].update(
            seen=len(rows), present=sum(1 for r in rows if r.get("present"))
        )
    if order == "fastest":
        rows.sort(key=lambda r: (-(r.get("kmh") or 0), r["km"]))
    elif order == "longest":
        rows.sort(key=lambda r: (-(r.get("len") or 0), r["km"]))
    unpainted = _choice(
        options.get("unpainted"),
        UNPAINTED,
        "photo" if widget in ("plate", "scene") else "silhouette",
    )
    only_painted = pictures == "paintings" and unpainted == "skip"
    shown = rows[: max(count, POOL) if only_painted else count]
    if not shown:
        return {
            **base,
            "state": "empty",
            "notices": [err] if err else [],
            "hint": "Nothing on the rails within reach right now. A wider radius would show more.",
        }

    # photographs: one vehicle's lookups beside the next's, none past the budget
    deadline = fetch_started + BUDGET_S

    def enrich(r: dict[str, Any]) -> dict[str, Any]:
        photo = R.photo_of(r, data_dir, ua)
        if photo.get("src"):
            N.cached_get(data_dir, photo["src"], ua=ua)
        # a side view of her class, for the painter: only when paintings are asked for
        side = R.side_of(r, data_dir, ua, own_sides) if pictures == "paintings" else {}
        return {"photo": photo, "side": side}

    deferred = 0
    tpool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="trackside-lookup")
    futures = {tpool.submit(enrich, r): r for r in shown}
    done, pending = wait(futures, timeout=max(0.2, deadline - time.time()))
    tpool.shutdown(wait=False)
    for f, r in futures.items():
        r.setdefault("photo", {})
        if f in done:
            try:
                r.update(f.result())
            except Exception:
                logger.exception("trackside: lookups for %s failed", r.get("num"))
                deferred += 1
        else:
            deferred += 1

    for r in shown:
        # "Frankston line, to Frankston" says it twice: the destination only when the line does not name it
        if r.get("dest") and r.get("line") and r["dest"].lower() in r["line"].lower():
            r["dest"] = ""
        # a train's set number (301M-1001T-302M) is long for the page's big figure: the leading car there, the set in the lines
        r["cs"] = r.get("car") or r["num"].split("-")[0]
        r["reg"] = r.get("line") or ""
        r["air"] = (
            "" if widget == "lineup" else r.get("op") or ""
        )  # a card has no room for it beside the number
        r["logo"] = ""
        r["route"] = {}
        r["route_text"] = " to ".join(
            x for x in (r.get("line") or r["typ"], r.get("dest") or "") if x
        )
        r["span"] = r.get("len") or 30
        r["compass"] = compass(r["dir"])
        r["where"] = where_of(r)
        r["lines"] = lines_for(r, labels)
        r["about"] = about_of(r)
        r["brand"] = BRANDS.get(r.get("op") or "", {}) if colours == "operator" else {}
        r["sil"] = (
            f"/plugins/trackside_core/static/sil/{r['cls'] if r['cls'] in ('tram', 'train') else 'train'}.svg"
        )

    credits: list[str] = []
    px = (
        PHOTO_PX
        if widget in ("photo", "scene")
        else HERO_PX
        if widget == "plate"
        else PICTURE_PX
    )
    whole_ = backdrop != "none" or style in P.OWN_PAPER
    no_photo = not_own = 0
    for r in shown:
        photo = r.get("photo") or {}
        exact = bool(photo.get("exact"))
        pic: dict[str, Any] | None = None
        if pictures == "paintings":
            if not photo.get("src"):
                no_photo += 1
            elif paint == "own" and not exact:
                not_own += 1
            else:
                who = (
                    f"{r['op']} {r['num']}"
                    if exact
                    else f"{r.get('klass') or r['typ']} class"
                )
                pkey = P.key_for(
                    who,
                    style,
                    backdrop,
                    view,
                    model,
                    weather,
                    fidelity,
                    photo="" if exact else photo["src"],
                    side=(r.get("side") or {}).get("src") or "",
                )
                payload = {
                    "num": r["num"],
                    "typ": r["typ"],
                    "op": r.get("op") or "",
                    "cls": r["cls"],
                    "exact": exact,
                    "style": style,
                    "how": how,
                    "backdrop": backdrop,
                    "weather_words": weather_text,
                    "view": view,
                    "fidelity": fidelity,
                    "model": model,
                    "photo": photo["src"],
                    "by": photo.get("by") or "",
                    "lic": photo.get("lic") or "",
                    "link": photo.get("link") or "",
                    "ua": ua,
                    "aspect": "4:3",
                    "side": (r.get("side") or {}).get("src") or "",
                    "side_by": (r.get("side") or {}).get("by") or "",
                    "side_lic": (r.get("side") or {}).get("lic") or "",
                }
                got = art.request(
                    pkey,
                    payload,
                    title=f"{r['typ']} {r['num']}"
                    if exact
                    else f"{r['typ']} (her class)",
                )
                if got.ready:
                    pic = P.picture(art, pkey, px, panel, whole_)
                    if pic:
                        pic["impression"] = not exact
                        if pic.get("credit"):
                            credits.append(pic["credit"])
                elif got.state == "failed":
                    r["why"] = got.why
        if (
            pic is None
            and photo.get("src")
            and not only_painted
            and (
                pictures == "photos"
                or unpainted == "photo"
                or not fal_key
                or widget in ("photo", "plate", "scene")
            )
        ):
            src = (
                R.photo_src(photo, data_dir, ua, px, panel)
                if time.time() < deadline + 1.2
                else ""
            )
            if not src:
                deferred += 1
            else:
                whose = "" if exact else ", a vehicle of her class"
                pic = {
                    "src": src,
                    "kind": "photo",
                    "whole": True,
                    "credit": f"photo by {photo.get('by') or 'a Commons contributor'} (Wikimedia Commons, {photo.get('lic') or 'open licence'}){whose}",
                    "aspect": N.aspect_of(src),
                    "link": photo.get("link") or "",
                    "impression": not exact,
                }
                credits.append(pic["credit"])
        r["pic"] = pic
        for k in ("lat", "lon", "photo", "cat", "side"):
            r.pop(k, None)
    if only_painted:
        shown = [
            r for r in shown if r.get("pic") and r["pic"].get("kind") == "painting"
        ][:count]
    else:
        shown = shown[:count]
    base["vehicles"] = shown
    base["shown"] = len(shown)

    notices: list[str] = []
    status: list[str] = []
    if base.get("watch"):
        wv = base["watch"]
        status.append(
            f"{wv['seen']} seen since {wv['since']}, {wv['present']} within reach now"
            if wv["seen"]
            else f"nothing seen since {wv['since']}"
        )
        if wv.get("error"):
            notices.append(f"the watch between renders: {wv['error']}")
    if pictures == "paintings":
        status.extend(art.status())
        notices.extend(art.notes())
        if no_photo:
            status.append(f"{no_photo} without a photograph to paint from")
        if not_own:
            status.append(f"{not_own} without a photograph of her own, left unpainted")
        art.finish(ctx, facet=style)
    if err:
        notices.append(err)
    live = art.live()
    if deferred:
        status.append(f"{deferred} picture{'' if deferred == 1 else 's'} on the way")
        live["live_refresh_s"] = max(live["live_refresh_s"], LIVE_S)
    if only_painted and not shown:
        waiting = sum(art.states.get(k, 0) for k in ("painting", "waiting", "held"))
        base.update(state="painting", notices=notices, status=status, **live)
        base["hint"] = (
            f"Painting {waiting} of the trains and trams near you; the page fills as they land."
            if waiting
            else P.NOTE_NO_KEY
            if art.states.get("no_key")
            else P.NEEDS_HOST
            if art.states.get("no_host")
            else "Nothing near you is painted yet."
        )
        return base
    base.update(
        notices=notices,
        status=status,
        credits=list(dict.fromkeys(credits))[:4],
        attribution=f"positions from {feed}" if feed else "",
        **live,
    )
    logger.info(
        "trackside: %s %s, %d near, %d shown, painted %d, started %d",
        "preview" if preview else "panel",
        widget,
        len(rows),
        len(shown),
        art.states.get("ready", 0),
        art.started,
    )
    return base
