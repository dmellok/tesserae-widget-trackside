"""The trains and trams near a place: GTFS-Realtime VehiclePositions feeds
(Transport Victoria's with a free key, or any agency's), the Melbourne fleet
by number, photographs of the very vehicle or of her class from Wikimedia
Commons, and the weather for a painted backdrop. Loaded by path."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import math
import re
import struct
import sys
import time
import urllib.parse
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


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

LIVE_S = (
    30  # Transport Victoria caches thirty seconds and allows 24 calls a minute a feed
)
FEED_TIMEOUT_S = 4.0
STALE_S = 15 * 60  # a vehicle not reported for this long is not on the page
PHOTO_S = 30 * 86400
CATEGORY_S = 14 * 86400
WEATHER_S = 15 * 60
COMMONS = "https://commons.wikimedia.org/w/api.php"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast?latitude={lat:.3f}&longitude={lon:.3f}&current=weather_code,cloud_cover,is_day&daily=sunrise,sunset&timezone=auto&forecast_days=1"
VIC = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/{feed}/vehicle-positions"


def user_agent(contact: str) -> str:
    c = " ".join(str(contact or "").split())
    return f"tesserae-trackside/0.1 (+https://github.com/dmellok/tesserae-widget-trackside{'; ' + c if c else ''})"


# ----------------------------------------------------------------------
# Where the positions come from
# ----------------------------------------------------------------------
# id -> label, [(feed url, mode)], key header, operator words for Commons, needs a key
PRESETS: dict[str, dict[str, Any]] = {
    "melbourne": {
        "label": "Melbourne: Metro Trains, Yarra Trams, V/Line (Transport Victoria, free key)",
        "feeds": [
            (VIC.format(feed="metro"), "train"),
            (VIC.format(feed="tram"), "tram"),
            (VIC.format(feed="vline"), "regional"),
        ],
        "header": "KeyID",
        "key": True,
        "operator": "",
        "city": "Melbourne",
        "credit": "Transport Victoria",
    },
    "boston": {
        "label": "Boston: MBTA subway, light rail and commuter rail (no key)",
        "feeds": [("https://cdn.mbta.com/realtime/VehiclePositions.pb", "")],
        "header": "",
        "key": False,
        "operator": "MBTA",
        "city": "Boston",
        "credit": "the MBTA",
    },
    "brisbane": {
        "label": "Brisbane: Queensland Rail City network (Translink, no key)",
        "feeds": [
            (
                "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Rail",
                "train",
            )
        ],
        "header": "",
        "key": False,
        "operator": "Queensland Rail",
        "city": "Brisbane",
        "credit": "Translink",
    },
    "goldcoast": {
        "label": "Gold Coast: G:link light rail (Translink, no key)",
        "feeds": [
            (
                "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Tram",
                "tram",
            )
        ],
        "header": "",
        "key": False,
        "operator": "G:link",
        "city": "Gold Coast",
        "credit": "Translink",
    },
}
SOURCES = ("ptv", *PRESETS, "custom")
PTV = "https://timetableapi.ptv.vic.gov.au"
PTV_STOPS_S = 86400  # the stops near a place hardly move
PTV_DUE_S = 20 * 60  # a tram due later than this is not yet on its way to the page


def feeds_for(
    source: str, options: dict[str, Any]
) -> tuple[list[tuple[str, str]], str, str]:
    """([(url, mode)], key header, operator words) for a cell: a preset's, or
    the cell's own feed links (one a line) for any agency."""
    if source in PRESETS:
        p = PRESETS[source]
        return list(p["feeds"]), p["header"], p["operator"]
    urls = [
        u.strip()
        for u in str(options.get("feed_urls") or "").splitlines()
        if u.strip().startswith(("http://", "https://"))
    ]
    return (
        [(u, "") for u in urls[:4]],
        str(options.get("key_header") or "").strip(),
        " ".join(str(options.get("operator") or "").split())[:60],
    )


# ----------------------------------------------------------------------
# GTFS-Realtime, read without a protobuf dependency
# ----------------------------------------------------------------------
def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    i, n = 0, len(buf)
    value: Any
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            if i + length > n:
                raise ValueError("truncated field")
            value, i = buf[i : i + length], i + length
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        else:
            raise ValueError(f"wire type {wire}")
        yield field, wire, value


def _subs(buf: bytes | None, field: int) -> list[bytes]:
    if not buf:
        return []
    return [v for f, w, v in _fields(buf) if f == field and w == 2]


def _sub(buf: bytes | None, field: int) -> bytes | None:
    got = _subs(buf, field)
    return got[0] if got else None


def _text(buf: bytes | None, field: int) -> str:
    got = _sub(buf, field)
    return got.decode("utf-8", "replace").strip() if got is not None else ""


def _int(buf: bytes | None, field: int) -> int | None:
    if not buf:
        return None
    for f, w, v in _fields(buf):
        if f == field and w == 0:
            return int(v)
    return None


def _float(buf: bytes | None, field: int) -> float | None:
    if not buf:
        return None
    for f, w, v in _fields(buf):
        if f == field and w == 5:
            got = struct.unpack("<f", v)[0]
            return None if math.isnan(got) else float(got)
        if f == field and w == 1:
            return float(struct.unpack("<d", v)[0])
    return None


STATUS = {0: "approaching a stop", 1: "at a stop", 2: "between stops"}


def decode(body: bytes, mode: str = "") -> list[dict[str, Any]]:
    """Every VehiclePosition in a FeedMessage with a position:
    {id, label, trip, route, dir, lat, lon, bearing, speed_kmh, status, stop, ts, mode}."""
    stamp = _int(_sub(body, 1), 3) or 0
    out = []
    for entity in _subs(body, 2):
        for v in _subs(entity, 4):
            pos = _sub(v, 2)
            lat, lon = _float(pos, 1), _float(pos, 2)
            if lat is None or lon is None or (abs(lat) < 0.01 and abs(lon) < 0.01):
                continue
            trip = _sub(v, 1)
            desc = _sub(v, 8)
            speed = _float(pos, 5)
            status = _int(v, 4)
            out.append(
                {
                    "id": _text(desc, 1) or _text(entity, 1),
                    "label": _text(desc, 2),
                    "trip": _text(trip, 1),
                    "route": _text(trip, 5),
                    "dir": _int(trip, 6),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "bearing": _float(pos, 3),
                    "speed_kmh": round(speed * 3.6, 1)
                    if speed is not None and 0 <= speed < 120
                    else None,
                    "status": STATUS.get(status if status is not None else 2, ""),
                    "stop": _text(v, 7),
                    "ts": _int(v, 5) or stamp,
                    "mode": mode,
                }
            )
    return out


# ----------------------------------------------------------------------
# Melbourne's fleets, by number
# ----------------------------------------------------------------------
# (first, last, class, name, length m, Commons category, about)
TRAMS: list[tuple[int, int, str, str, float, str, str]] = [
    (
        9,
        95,
        "Z1",
        "Z1-class tram",
        16.6,
        "Z class trams",
        "a high-floor Comeng tram of the 1970s, one body on two bogies",
    ),
    (
        101,
        115,
        "Z2",
        "Z2-class tram",
        16.6,
        "Z class trams",
        "a high-floor Comeng tram of the 1970s, one body on two bogies",
    ),
    (
        116,
        230,
        "Z3",
        "Z3-class tram",
        16.6,
        "Z class trams",
        "a high-floor Comeng tram of the late 1970s, one body on two bogies",
    ),
    (
        231,
        258,
        "A1",
        "A1-class tram",
        15.0,
        "A class trams",
        "a short high-floor Comeng tram of the 1980s",
    ),
    (
        259,
        300,
        "A2",
        "A2-class tram",
        15.0,
        "A class trams",
        "a short high-floor Comeng tram of the 1980s",
    ),
    (
        856,
        1010,
        "W8",
        "W8-class tram",
        14.7,
        "W class trams",
        "a heritage W-class tram, the green-and-cream drop-centre car of 1950s Melbourne",
    ),
    (
        1020,
        1020,
        "W7",
        "W7-class tram",
        14.7,
        "W class trams",
        "a heritage W-class tram",
    ),
    (
        2003,
        2132,
        "B2",
        "B2-class tram",
        23.6,
        "B class Melbourne trams",
        "an articulated high-floor Comeng tram of two sections",
    ),
    (
        3001,
        3036,
        "C",
        "C-class Citadis tram",
        22.7,
        "Citadis 202 trams in Melbourne",
        "an Alstom Citadis low-floor tram of three sections",
    ),
    (
        3501,
        3538,
        "D1",
        "D1-class Combino tram",
        20.0,
        "Combino trams in Melbourne",
        "a Siemens Combino low-floor tram of three sections",
    ),
    (
        5001,
        5021,
        "D2",
        "D2-class Combino tram",
        29.8,
        "Combino trams in Melbourne",
        "a Siemens Combino low-floor tram of five sections",
    ),
    (
        5103,
        5123,
        "C2",
        "C2-class Citadis tram",
        32.5,
        "Citadis 302 trams in Melbourne",
        "an Alstom Citadis low-floor tram of five sections, the 'Bumblebee'",
    ),
    (
        6001,
        6050,
        "E",
        "E-class tram",
        33.4,
        "E class trams",
        "a Bombardier Flexity low-floor tram of three sections, built in Dandenong",
    ),
    (
        6051,
        6100,
        "E2",
        "E2-class tram",
        33.4,
        "E class trams",
        "a Bombardier Flexity low-floor tram of three sections, built in Dandenong",
    ),
    (
        7001,
        7100,
        "G",
        "G-class tram",
        33.0,
        "G class trams",
        "an Alstom Flexity 2 low-floor tram, Melbourne's newest",
    ),
]
# motor and trailer cars: (first, last, suffix, class, name, Commons category, about)
TRAINS: list[tuple[int, int, str, str, str, str, str]] = [
    (
        1,
        288,
        "M",
        "xtrap",
        "X'Trapolis 100",
        "X'Trapolis 100 in Melbourne",
        "an Alstom X'Trapolis 100 electric suburban train",
    ),
    (
        851,
        986,
        "M",
        "xtrap",
        "X'Trapolis 100",
        "X'Trapolis 100 in Melbourne",
        "an Alstom X'Trapolis 100 electric suburban train",
    ),
    (
        1301,
        1444,
        "T",
        "xtrap",
        "X'Trapolis 100",
        "X'Trapolis 100 in Melbourne",
        "an Alstom X'Trapolis 100 electric suburban train",
    ),
    (
        1626,
        1693,
        "T",
        "xtrap",
        "X'Trapolis 100",
        "X'Trapolis 100 in Melbourne",
        "an Alstom X'Trapolis 100 electric suburban train",
    ),
    (
        301,
        700,
        "M",
        "comeng",
        "Comeng train",
        "Comeng (train)",
        "a Comeng stainless-steel electric suburban train of the 1980s",
    ),
    (
        1001,
        1200,
        "T",
        "comeng",
        "Comeng train",
        "Comeng (train)",
        "a Comeng stainless-steel electric suburban train of the 1980s",
    ),
    (
        701,
        844,
        "M",
        "nexas",
        "Siemens Nexas",
        "Siemens Nexas",
        "a Siemens Nexas electric suburban train",
    ),
    (
        2501,
        2572,
        "T",
        "nexas",
        "Siemens Nexas",
        "Siemens Nexas",
        "a Siemens Nexas electric suburban train",
    ),
]
TRAIN_LEN = {
    "xtrap": 70.0,
    "comeng": 71.0,
    "nexas": 72.0,
    "hcmt": 160.0,
    "vlocity": 75.0,
}
HCMT = (
    "hcmt",
    "High Capacity Metro Train",
    "High Capacity Metro Trains",
    "an Evolution high-capacity seven-car electric suburban train",
)
VLOCITY = (
    "vlocity",
    "VLocity",
    "VLocity",
    "a VLocity diesel regional railcar of V/Line",
)
TRAM_COLOUR = "Yarra Trams' green and white"
# Metro Trains Melbourne's lines by the codes in their route ids
METRO_LINES = {
    "ALM": "Alamein",
    "BEG": "Belgrave",
    "CBE": "Cranbourne",
    "CGB": "Craigieburn",
    "FKN": "Frankston",
    "GWY": "Glen Waverley",
    "HBE": "Hurstbridge",
    "LIL": "Lilydale",
    "MDD": "Mernda",
    "PKM": "Pakenham",
    "SDM": "Sandringham",
    "STY": "Stony Point",
    "SUY": "Sunbury",
    "UFD": "Upfield",
    "WER": "Werribee",
    "WIL": "Williamstown",
    "RCE": "Flemington Racecourse",
    "CCL": "City Circle",
}


def _numbers(text: str) -> list[tuple[int, str]]:
    """The fleet numbers in a label, with an M or T suffix where it has one:
    '301M-1001T-302M' gives [(301, 'M'), (1001, 'T'), (302, 'M')]."""
    return [
        (int(n), s.upper())
        for n, s in re.findall(r"(?<!\d)(\d{1,4})([MTmt]?)(?![\d])", text or "")
    ]


def melbourne_vehicle(mode: str, label: str, vid: str) -> dict[str, Any]:
    """{num, cls, typ, len, cat, about, op} for a Melbourne vehicle from its
    label (or id); the class unknown when the number fits none."""
    nums = _numbers(label) or _numbers(vid)
    if mode == "tram":
        for n, _s in nums:
            for lo, hi, cls, name, length, cat, about in TRAMS:
                if lo <= n <= hi:
                    return {
                        "num": str(n),
                        "cls": "tram",
                        "typ": name,
                        "klass": cls,
                        "len": length,
                        "cat": cat,
                        "about": about,
                        "op": "Yarra Trams",
                    }
        return {
            "num": str(nums[0][0]) if nums else (label or vid),
            "cls": "tram",
            "typ": "Tram",
            "klass": "",
            "len": 25.0,
            "cat": "",
            "about": "a Melbourne tram",
            "op": "Yarra Trams",
        }
    if mode == "regional":
        n = nums[0][0] if nums else None
        return {
            "num": str(n) if n else (label or vid),
            "cls": "train",
            "typ": "VLocity railcar" if n else "V/Line train",
            "klass": VLOCITY[0] if n else "",
            "len": TRAIN_LEN["vlocity"],
            "cat": VLOCITY[2] if n else "",
            "about": VLOCITY[3] if n else "a V/Line regional train",
            "op": "V/Line",
        }
    cars = [f"{n}{s}" for n, s in nums]
    for n, s in nums:
        for lo, hi, suffix, cls, name, cat, about in TRAINS:
            if lo <= n <= hi and (not s or s == suffix):
                return {
                    "num": "-".join(cars[:3]) if len(cars) > 1 else f"{n}{s or suffix}",
                    "cls": "train",
                    "typ": name,
                    "klass": cls,
                    "len": TRAIN_LEN[cls],
                    "cat": cat,
                    "about": about,
                    "op": "Metro Trains Melbourne",
                    "car": f"{n}{s or suffix}",
                }
    if nums and 9000 <= nums[0][0] <= 9999:
        return {
            "num": cars[0],
            "cls": "train",
            "typ": HCMT[1],
            "klass": HCMT[0],
            "len": TRAIN_LEN["hcmt"],
            "cat": HCMT[2],
            "about": HCMT[3],
            "op": "Metro Trains Melbourne",
        }
    return {
        "num": cars[0] if cars else (label or vid),
        "cls": "train",
        "typ": "Metro train",
        "klass": "",
        "len": 70.0,
        "cat": "",
        "about": "a Melbourne suburban train",
        "op": "Metro Trains Melbourne",
    }


def melbourne_route(mode: str, route: str) -> str:
    """'Route 96', 'Frankston line', or the route id as it came."""
    tokens = [t for t in re.split(r"[-:_ ]+", route or "") if t]
    if mode == "train":
        for t in tokens:
            if t.upper() in METRO_LINES:
                return f"{METRO_LINES[t.upper()]} line"
    if mode == "regional":
        return ""  # V/Line's route ids name no line a reader would know
    if mode == "tram":
        for i, t in enumerate(tokens):
            if (
                t in ("3", "03")
                and i + 1 < len(tokens)
                and re.fullmatch(r"\d{1,3}[a-z]?", tokens[i + 1])
            ):
                return f"Route {tokens[i + 1].lstrip('0')}"
        if len(tokens) == 1 and re.fullmatch(r"\d{1,3}[a-z]?", tokens[0]):
            return f"Route {tokens[0]}"
    return route


# ----------------------------------------------------------------------
# Elsewhere: the mode and the line from the route id, where a preset knows
# ----------------------------------------------------------------------
def boston_vehicle(route: str, label: str) -> tuple[str, str, str, float]:
    """(mode, line, class name, length m) for an MBTA vehicle."""
    if route.startswith("CR-"):
        return "train", f"{route[3:].replace('-', ' ')} Line", "commuter train", 85.0
    if route.startswith("Green-"):
        return "tram", f"Green Line {route[6:]}", "Green Line light rail car", 22.0
    if route == "Mattapan":
        return "tram", "Mattapan Trolley", "PCC streetcar", 14.0
    if route in ("Red", "Orange", "Blue"):
        return "subway", f"{route} Line", f"{route} Line subway car", 20.0
    return "bus", f"Route {route}" if route else "", "bus", 12.0


def generic_vehicle(
    mode: str, route: str, label: str, vid: str, operator: str
) -> dict[str, Any]:
    num = label or vid
    if operator == "MBTA":
        m, line, typ, length = boston_vehicle(route, label)
        return {
            "num": num,
            "cls": "tram" if m == "tram" else "bus" if m == "bus" else "train",
            "mode": m,
            "line": line,
            "typ": typ,
            "len": length,
            "about": f"an MBTA {typ}",
            "op": "MBTA",
        }
    cls = "tram" if mode == "tram" else "train"
    words = {"tram": "tram", "train": "train", "regional": "train"}.get(mode, "train")
    return {
        "num": num,
        "cls": cls,
        "mode": mode or "train",
        "line": route.split("-")[0] if route else "",
        "typ": f"{operator} {words}".strip() if operator else words.capitalize(),
        "len": 25.0 if cls == "tram" else 70.0,
        "about": f"a {operator + ' ' if operator else ''}{words}",
        "op": operator,
    }


# ----------------------------------------------------------------------
# The vehicles near a place
# ----------------------------------------------------------------------
def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * r * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360) % 360


def _feed(
    url: str, mode: str, header: str, key: str, data_dir: Path, ua: str
) -> tuple[list[dict[str, Any]], str | None]:
    """One feed, kept LIVE_S; the last good answer when it cannot be read."""
    cache = data_dir / "live" / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")
    got = N.read_json(cache, None)
    if isinstance(got, dict) and time.time() - float(got.get("at") or 0) < LIVE_S:
        return got.get("rows") or [], None
    headers = {header: key} if header and key else {}
    body, err = N.http(
        url,
        accept="application/x-protobuf, application/octet-stream, */*",
        timeout=FEED_TIMEOUT_S,
        ua=ua,
        headers=headers,
    )
    rows: list[dict[str, Any]] | None = None
    if body is not None:
        try:
            rows = decode(body, mode)
        except (ValueError, struct.error, IndexError):
            err = "not a GTFS-Realtime feed"
    if rows is None:
        return (got.get("rows") or []) if isinstance(
            got, dict
        ) else [], err or "unreachable"
    N.write(cache, json.dumps({"at": time.time(), "rows": rows}).encode())
    return rows, None


# ----------------------------------------------------------------------
# Melbourne through the PTV Timetable API (a developer id and key, each
# request signed): trains with their live position, their cars and where
# they are going; trams by fleet number at the stop they are due at.
# ----------------------------------------------------------------------
TRAIN_WORDS = (
    ("siemens", "nexas"),
    ("comeng", "comeng"),
    ("trapolis", "xtrap"),
    ("hcmt", "hcmt"),
    ("high capacity", "hcmt"),
    ("evolution", "hcmt"),
)


def ptv_url(path: str, devid: str, key: str) -> str:
    """A signed Timetable API link: HMAC-SHA1 of the path and query, devid included, in capitals."""
    import hmac

    p = path + ("&" if "?" in path else "?") + f"devid={devid}"
    sig = hmac.new(key.encode(), p.encode(), hashlib.sha1).hexdigest().upper()
    return f"{PTV}{p}&signature={sig}"


def _ptv(path: str, devid: str, key: str, ua: str) -> tuple[Any, str | None]:
    return N.get_json(ptv_url(path, devid, key), ua=ua, timeout=FEED_TIMEOUT_S)


def ptv_train(desc: dict[str, Any]) -> dict[str, Any]:
    """A Metro train from the run's vehicle descriptor: its cars ("1178T-1179T-655M-..."),
    and its class from the operator's own description ("6 Car Siemens") before the car numbers."""
    cars = str(desc.get("id") or "")
    words = str(desc.get("description") or "")
    info = melbourne_vehicle("train", cars, "")
    klass = next((k for w, k in TRAIN_WORDS if w in words.lower()), "")
    if klass and klass != info.get("klass"):
        name, cat, about = next(
            ((n, c, a) for _lo, _hi, _s, k, n, c, a in TRAINS if k == klass),
            (HCMT[1], HCMT[2], HCMT[3]),
        )
        info.update(
            klass=klass, typ=name, cat=cat, about=about, len=TRAIN_LEN.get(klass, 70.0)
        )
    m = re.match(r"\s*(\d+)\s*car", words, re.I)
    if m:
        info["typ"] = f"{info['typ']}, {m.group(1)} cars"
    motors = [c for c in re.split(r"[-+ ]+", cars) if c.upper().endswith("M")]
    info["car"] = motors[0].upper() if motors else info.get("car", "")
    info["num"] = cars or info["num"]
    return info


def nearby_ptv(
    key: str, lat: float, lon: float, radius_km: float, data_dir: Path, ua: str
) -> tuple[list[dict[str, Any]], str | None, str]:
    """(rows, error, feed name) from the PTV Timetable API. ``key`` is "devid:key"."""
    devid, _, secret = (key or "").partition(":")
    if not devid.strip() or not secret.strip():
        return (
            [],
            "Give your PTV Timetable API developer id and key in the Trackside Core settings.",
            "",
        )
    devid, secret = devid.strip(), secret.strip()
    cache = data_dir / "live" / f"ptv_{lat:.4f}_{lon:.4f}_{radius_km:g}.json"
    got = N.read_json(cache, None)
    if isinstance(got, dict) and time.time() - float(got.get("at") or 0) < LIVE_S:
        return got.get("rows") or [], got.get("err"), "PTV"
    # the stops: kept a day
    spath = data_dir / "live" / f"ptv_stops_{lat:.4f}_{lon:.4f}_{radius_km:g}.json"
    stops = N.read_json(spath, None)
    if not (
        isinstance(stops, dict)
        and time.time() - float(stops.get("at") or 0) < PTV_STOPS_S
    ):
        dist = int(min(max(radius_km, 0.5), 5) * 1000)
        data, err = _ptv(
            f"/v3/stops/location/{lat:.5f},{lon:.5f}?route_types=0&route_types=1&max_results=12&max_distance={dist}",
            devid,
            secret,
            ua,
        )
        if data is None:
            why = (
                "refused the developer id and key"
                if err == "auth"
                else (err or "gave no answer")
            )
            return [], f"The PTV Timetable API {why}.", ""
        stops = {
            "at": time.time(),
            "stops": [s for s in data.get("stops") or [] if isinstance(s, dict)],
        }
        N.write(spath, json.dumps(stops).encode())
    rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    trains = [s for s in stops["stops"] if s.get("route_type") == 0][:2]
    trams = [s for s in stops["stops"] if s.get("route_type") == 1][:4]
    now = time.time()
    asks = [
        (
            s,
            0,
            f"/v3/departures/route_type/0/stop/{s['stop_id']}?max_results=6&expand=VehiclePosition&expand=VehicleDescriptor&expand=Run&expand=Route",
        )
        for s in trains
    ]
    asks += [
        (
            s,
            1,
            f"/v3/departures/route_type/1/stop/{s['stop_id']}?max_results=2&expand=VehicleDescriptor&expand=Run&expand=Route",
        )
        for s in trams
    ]
    pool = ThreadPoolExecutor(
        max_workers=max(1, len(asks)), thread_name_prefix="trackside-ptv"
    )
    futures = {
        pool.submit(_ptv, path, devid, secret, ua): (s, rt) for s, rt, path in asks
    }
    done, _pending = wait(futures, timeout=FEED_TIMEOUT_S + 1.5)
    pool.shutdown(wait=False)
    for f, (stop, rt) in futures.items():
        if f not in done:
            errors.append("too slow")
            continue
        data, err = f.result()
        if data is None:
            errors.append(
                "refused the developer id and key"
                if err == "auth"
                else err or "no answer"
            )
            continue
        runs, routes = data.get("runs") or {}, data.get("routes") or {}
        for dep in data.get("departures") or []:
            due = ""
            run = runs.get(str(dep.get("run_ref"))) or {}
            route = routes.get(str(dep.get("route_id"))) or {}
            desc = run.get("vehicle_descriptor") or {}
            dest = " ".join(str(run.get("destination_name") or "").split())
            if rt == 0:
                pos = run.get("vehicle_position") or {}
                if not isinstance(pos.get("latitude"), (int, float)) or not desc.get(
                    "id"
                ):
                    continue
                info = ptv_train(desc)
                vlat, vlon = float(pos["latitude"]), float(pos["longitude"])
                line = (
                    f"{route.get('route_name') or ''} line".strip()
                    if route.get("route_name")
                    else ""
                )
                status = ""  # the API says where a train is, not whether she is at a platform
                trk = (
                    int(round(float(pos["bearing"]))) % 360
                    if isinstance(pos.get("bearing"), (int, float))
                    else -1
                )
                due = ""
            else:
                if not desc.get("id"):
                    continue
                when = dep.get("estimated_departure_utc") or dep.get(
                    "scheduled_departure_utc"
                )
                with contextlib.suppress(ValueError, TypeError):
                    from datetime import datetime

                    mins = (
                        datetime.fromisoformat(
                            str(when).replace("Z", "+00:00")
                        ).timestamp()
                        - now
                    ) / 60
                    if mins * 60 > PTV_DUE_S or mins < -1:
                        continue
                    stop_name = re.sub(
                        r"\s*#\d+\s*$",
                        "",
                        " ".join(str(stop.get("stop_name") or "").split()),
                    )
                    due = (
                        f"at {stop_name}"
                        if mins < 1
                        else f"due at {stop_name} in {int(round(mins))} min"
                    )
                if not due:
                    continue
                info = melbourne_vehicle("tram", str(desc["id"]), "")
                vlat, vlon = (
                    float(stop.get("stop_latitude") or lat),
                    float(stop.get("stop_longitude") or lon),
                )
                line = (
                    f"Route {route['route_number']}"
                    if route.get("route_number")
                    else ""
                )
                status = due
                trk = -1
            km, deg = _bearing(lat, lon, vlat, vlon)
            if km > radius_km:
                continue
            vid = str(desc.get("id"))
            row = {
                **info,
                "vid": vid,
                "label": vid,
                "mode": "train" if rt == 0 else "tram",
                "route_id": str(route.get("route_gtfs_id") or ""),
                "line": line,
                "dest": re.sub(r"\s*#\d+\s*$", "", dest)
                .replace(" Railway Station", "")
                .strip(),
                "trip": str(dep.get("run_ref") or ""),
                "lat": vlat,
                "lon": vlon,
                "km": round(km, 1),
                "dir": int(round(deg)) % 360,
                "trk": trk,
                "kmh": None,
                "status": status,
                "stop": str(stop.get("stop_id") or ""),
                "age_s": 0,
                "city": "Melbourne",
            }
            old = rows.get(vid)
            if old is None or (rt == 1 and row["km"] < old["km"]):
                rows[vid] = row
    out = sorted(rows.values(), key=lambda r: r["km"])
    err = "the PTV Timetable API: " + "; ".join(sorted(set(errors))) if errors else None
    if err and not out and len(errors) == len(asks):
        return [], err[0].upper() + err[1:] + ".", ""
    N.write(cache, json.dumps({"at": time.time(), "rows": out, "err": err}).encode())
    return out, err, "PTV"


def nearby(
    source: str,
    options: dict[str, Any],
    key: str,
    lat: float,
    lon: float,
    radius_km: float,
    data_dir: Path,
    ua: str,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """(rows, error, feed name): the trains and trams within ``radius_km`` of a
    point, nearest first."""
    if source == "ptv":
        return nearby_ptv(key, lat, lon, radius_km, data_dir, ua)
    feeds, header, operator = feeds_for(source, options)
    if not feeds:
        return (
            [],
            "Give the link of a GTFS-Realtime VehiclePositions feed, one a line.",
            "",
        )
    if PRESETS.get(source, {}).get("key") and not key:
        return (
            [],
            "Give a Transport Victoria open data key in the Trackside Core settings (free at opendata.transport.vic.gov.au).",
            "",
        )
    if source == "custom" and header and not key:
        key = str(options.get("feed_key") or "").strip()
    pool = ThreadPoolExecutor(
        max_workers=len(feeds), thread_name_prefix="trackside-feed"
    )
    futures = {
        pool.submit(_feed, u, m, header, key, data_dir, ua): (u, m) for u, m in feeds
    }
    done, _pending = wait(futures, timeout=FEED_TIMEOUT_S + 1.5)
    pool.shutdown(wait=False)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for f, (u, _m) in futures.items():
        if f not in done:
            errors.append(f"{urllib.parse.urlsplit(u).hostname}: too slow")
            continue
        rows, err = f.result()
        records += rows
        if err:
            errors.append(
                f"{urllib.parse.urlsplit(u).hostname}: {'refused the key' if err == 'auth' else err}"
            )
    now = time.time()
    out = []
    for v in records:
        if v.get("ts") and now - float(v["ts"]) > STALE_S:
            continue
        km, deg = _bearing(lat, lon, v["lat"], v["lon"])
        if km > radius_km:
            continue
        mode = v.get("mode") or ""
        if source == "melbourne":
            info = melbourne_vehicle(mode, v.get("label") or "", v.get("id") or "")
            line = melbourne_route(mode, v.get("route") or "")
        else:
            info = generic_vehicle(
                mode,
                v.get("route") or "",
                v.get("label") or "",
                v.get("id") or "",
                operator,
            )
            mode = info.pop("mode")
            line = info.pop("line")
        if info["cls"] == "bus":
            continue
        out.append(
            {
                **info,
                "vid": v.get("id") or "",
                "label": v.get("label") or "",
                "mode": mode or info["cls"],
                "route_id": v.get("route") or "",
                "line": line,
                "trip": v.get("trip") or "",
                "lat": v["lat"],
                "lon": v["lon"],
                "km": round(km, 1),
                "dir": int(round(deg)) % 360,
                "trk": int(round(v["bearing"])) % 360
                if isinstance(v.get("bearing"), (int, float))
                else -1,
                "kmh": v.get("speed_kmh"),
                "status": v.get("status") or "",
                "stop": v.get("stop") or "",
                "age_s": int(max(0, now - float(v.get("ts") or now))),
                "city": PRESETS.get(source, {}).get("city", ""),
            }
        )
    out.sort(key=lambda r: r["km"])
    err = "; ".join(errors) if errors else None
    if err and not out and len(errors) == len(feeds):
        return [], f"The live feeds could not be read ({err}).", ""
    name = (
        PRESETS[source]["credit"] if source in PRESETS else (operator or "your feeds")
    )
    return out, err, name


# ----------------------------------------------------------------------
# Photographs: Wikimedia Commons, the very vehicle by its fleet number in the
# file title within her class's category, else the best picture of the class
# ----------------------------------------------------------------------
NOT_A_PORTRAIT = (
    "interior",
    "inside",
    "seat",
    "cab ",
    "driver",
    "plaque",
    "sign",
    "map",
    "mockup",
    "mock-up",
    "diagram",
    "ticket",
    "myki",
    "screen",
    "display",
    "logo",
    "bogie",
    "pantograph",
    "coupler",
    "door",
    "window",
    "accident",
    "crash",
    "derail",
    "scrap",
    "fire",
    "graffiti",
    "model",
    "toy",
    "render",
    "night",
)


def _commons(params: dict[str, str], ua: str) -> Any:
    data, _err = N.get_json(
        f"{COMMONS}?{urllib.parse.urlencode({**params, 'format': 'json', 'formatversion': '2'})}",
        ua=ua,
        timeout=5,
    )
    return data


def category_files(cat: str, data_dir: Path, ua: str) -> list[str]:
    """The file titles in a Commons category, kept a fortnight."""
    path = data_dir / "photos" / f"cat_{N.slug(cat)}.json"
    got = N.read_json(path, None)
    if isinstance(got, dict) and time.time() - float(got.get("at") or 0) < CATEGORY_S:
        return list(got.get("files") or [])
    files: list[str] = []
    cont: dict[str, str] = {}
    for _ in range(4):
        data = _commons(
            {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": f"Category:{cat}",
                "cmnamespace": "6",
                "cmlimit": "500",
                **cont,
            },
            ua,
        )
        if not isinstance(data, dict):
            break
        files += [
            m["title"]
            for m in (data.get("query") or {}).get("categorymembers") or []
            if isinstance(m, dict) and m.get("title")
        ]
        if "continue" not in data:
            break
        cont = {"cmcontinue": str(data["continue"].get("cmcontinue") or "")}
    if files or not isinstance(got, dict):
        N.write(path, json.dumps({"at": time.time(), "files": files}).encode())
    return files or list((got or {}).get("files") or [])


def _portrait(title: str) -> bool:
    t = title.lower()
    return t.endswith((".jpg", ".jpeg", ".png", ".webp")) and not any(
        w in t for w in NOT_A_PORTRAIT
    )


CROWDED = (
    " and ",
    "&",
    "multiple",
    " with ",
    " meets ",
    " passes ",
    " pass ",
    "crossing",
    " x ",
    "fleet",
    "lineup",
    "yard",
)


def _number_patterns(token: str) -> list[re.Pattern[str]]:
    """How a fleet number may be written in a file title: '706M' as itself
    and as 706; a number of one or two digits only after a word that says it
    is a fleet number (No. 2, car 14, tram #8), never a stray year or count."""
    m = re.fullmatch(r"(\d{1,4})([MT]?)", token.upper())
    if not m:
        return []
    digits, suffix = m.groups()
    pats = []
    if suffix:
        pats.append(
            re.compile(rf"(?<!\d){digits}{suffix}(?![A-Za-z\d])", re.IGNORECASE)
        )
    if len(digits) >= 3:
        pats.append(re.compile(rf"(?<!\d){digits}(?![\d])"))
    else:
        pats.append(
            re.compile(
                rf"(?:\bno\.?|#|\bcar|\btram|\bunit|\bset|\bnumber)\s*{digits}(?!\d)",
                re.IGNORECASE,
            )
        )
    return pats


def pick_file(files: list[str], num: str, car: str = "") -> tuple[str, bool]:
    """(title, exact): a file naming one of the vehicle's numbers, else the
    class's best plain picture of one vehicle (a recent-looking dated title first)."""
    tokens = []
    for t in [car, *re.split(r"[-+/ ]+", num)]:
        if t and t.upper() not in tokens:
            tokens.append(t.upper())
    for tok in tokens:
        for pat in _number_patterns(tok):
            hits = [
                f for f in files if _portrait(f) and pat.search(f.split(":", 1)[-1])
            ]
            if hits:
                hits.sort(
                    key=lambda f: (
                        any(w in f.lower() for w in CROWDED),
                        " at " not in f,
                        len(f),
                    )
                )
                return hits[0], True
    pool = [
        f for f in files if _portrait(f) and not any(w in f.lower() for w in CROWDED)
    ]
    if not pool:
        return "", False
    # another vehicle of the class by her number first: a title that names one is a picture of one
    fleet = re.compile(r"(?<![\d.])(?!19\d\d|20\d\d)\d{3,4}[MT]?(?![\d])")
    pool.sort(
        key=lambda f: (
            not fleet.search(f.split(":", 1)[-1]),
            not re.search(r"20[12]\d", f),
            " at " not in f,
            f,
        )
    )
    return pool[0], False


def file_info(title: str, data_dir: Path, ua: str) -> dict[str, str]:
    path = (
        data_dir
        / "photos"
        / f"file_{hashlib.sha1(title.encode()).hexdigest()[:16]}.json"
    )
    got = N.read_json(path, None)
    if isinstance(got, dict) and time.time() - float(got.get("at") or 0) < PHOTO_S:
        return got.get("photo") or {}
    data = _commons(
        {
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size",
            "iiurlwidth": "1600",
        },
        ua,
    )
    photo: dict[str, str] = {}
    with contextlib.suppress(KeyError, TypeError, IndexError):
        ii = data["query"]["pages"][0]["imageinfo"][0]
        meta = ii.get("extmetadata") or {}
        by = re.sub(
            r"<[^>]+>", "", str((meta.get("Artist") or {}).get("value") or "")
        ).strip()
        lic = str((meta.get("LicenseShortName") or {}).get("value") or "").strip()
        if "ND" not in lic.upper().split("-") and "nd" not in lic.lower().replace(
            " ", "-"
        ).split("-"):
            photo = {
                "src": str(ii.get("thumburl") or ii.get("url") or ""),
                "by": by[:80] or "a Commons contributor",
                "lic": lic or "open licence",
                "link": str(ii.get("descriptionurl") or ""),
                "title": title.split(":", 1)[-1],
            }
    if data is not None:
        N.write(path, json.dumps({"at": time.time(), "photo": photo}).encode())
    return photo


def search_files(words: str, data_dir: Path, ua: str) -> list[str]:
    """File titles for a Commons search, kept a fortnight: for an agency
    without a class table, the operator's name and the fleet number."""
    path = data_dir / "photos" / f"q_{N.slug(words)}.json"
    got = N.read_json(path, None)
    if isinstance(got, dict) and time.time() - float(got.get("at") or 0) < CATEGORY_S:
        return list(got.get("files") or [])
    data = _commons(
        {
            "action": "query",
            "list": "search",
            "srsearch": words,
            "srnamespace": "6",
            "srlimit": "30",
        },
        ua,
    )
    files = (
        [
            h["title"]
            for h in ((data or {}).get("query") or {}).get("search") or []
            if isinstance(h, dict)
        ]
        if isinstance(data, dict)
        else []
    )
    if data is not None:
        N.write(path, json.dumps({"at": time.time(), "files": files}).encode())
    return files


def photo_of(r: dict[str, Any], data_dir: Path, ua: str) -> dict[str, str]:
    """{src, by, lic, link, title, exact} for the vehicle: her own photograph
    when Commons has one by her number, else one of her class; {} when
    neither is found."""
    num, car = str(r.get("num") or ""), str(r.get("car") or "")
    if r.get("cat"):
        files = category_files(r["cat"], data_dir, ua)
    elif r.get("op") and num:
        first = re.split(r"[-+/ ]+", num)[0]
        files = search_files(f'"{r["op"]}" {first}', data_dir, ua)
        title, exact = pick_file(files, first)
        if not exact:
            return {}  # a search's other hits are other vehicles, not her class
        photo = file_info(title, data_dir, ua)
        return {**photo, "exact": "1"} if photo.get("src") else {}
    else:
        return {}
    title, exact = pick_file(files, num, car)
    if not title:
        return {}
    photo = file_info(title, data_dir, ua)
    return {**photo, "exact": "1" if exact else ""} if photo.get("src") else {}


# ----------------------------------------------------------------------
# Side views: one picture of each class seen from the side, chosen by hand
# from Wikimedia Commons (most photographs of a tram or a train are taken
# from the front three-quarter), for the painter to take her length, her
# sections and the spacing of her doors from.
# ----------------------------------------------------------------------
SIDE_VIEWS: dict[str, str] = {
    "Z": "160 at Cotham Rd, Burke Rd.jpg",
    "A": "A1 237 (Melbourne tram) in Bourke St, December 2013.JPG",
    "B2": "2038 at Federation Square, Swanston St.jpg",
    "C": "C-class tram -3009 operating Route 48 to Victoria Harbour Docklands on Collins Street crossing Swanston Street, Melbourne.jpg",
    "C2": "C2.5123 bourke, 2014.jpg",
    "D1": "D-class Melbourne tram. (21123049648).jpg",
    "D2": "D Class tram on Sydney Road.jpg",
    "E": "Bombarider Flexity Swift E1 Class Tram No.6033 (55396907756).jpg",
    "G": "Side view of G class tram number 7006 in PTV livery operating a route 59 service to Flinders Street Station at Flinders Street Station terminus on the day of the first run of the G class tram in revenue service, Melbourne.jpg",
    "W": "Melbourne tram at Muni Heritage Weekend line scan dllu.jpg",
    "comeng": "630 at Flinders Street.jpg",
    "nexas": "731 at South Yarra.jpg",
    "xtrap": "210M(Cropped).jpg",
    "hcmt": "904 at Malvern.jpg",
    "vlocity": "Melton VLine Train October 2018.JPG",
}


def side_key(r: dict[str, Any]) -> str:
    """The side-view table's key for a vehicle: Z1, Z2 and Z3 share the Z's, E2 the E's."""
    k = str(r.get("klass") or "")
    return {
        "Z1": "Z",
        "Z2": "Z",
        "Z3": "Z",
        "A1": "A",
        "A2": "A",
        "E2": "E",
        "W7": "W",
        "W8": "W",
    }.get(k, k)


def own_side_views(text: Any) -> dict[str, str]:
    """The owner's own side views, one a line: ``class = URL``. The class is
    the widget's (E, C2, comeng, xtrap...) or the class name the page shows."""
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if "=" in line:
            k, _, src = line.partition("=")
            if k.strip() and src.strip().startswith(("http://", "https://")):
                out[k.strip().lower()] = src.strip()
    return out


def side_of(
    r: dict[str, Any], data_dir: Path, ua: str, own: dict[str, str] | None = None
) -> dict[str, str]:
    """{src, by, lic, link} for a side view of the vehicle's class: the
    owner's own first, then the table's; {} when there is none."""
    own = own or {}
    for k in (
        side_key(r).lower(),
        str(r.get("klass") or "").lower(),
        str(r.get("typ") or "").lower(),
    ):
        if k and k in own:
            return {"src": own[k], "by": "your own picture", "lic": "", "link": own[k]}
    title = SIDE_VIEWS.get(side_key(r))
    if not title:
        return {}
    got = file_info(f"File:{title}", data_dir, ua)
    return got if got.get("src") else {}


def photo_src(
    photo: dict[str, str], data_dir: Path, ua: str, px: int, levels: str
) -> str:
    url = photo.get("src") or ""
    if not url:
        return ""
    raw = N.cached_get(data_dir, url, ua=ua)
    if not raw:
        return ""
    cache = (
        data_dir / "img" / (hashlib.sha1(url.encode()).hexdigest()[:20] + f"_{px}.jpg")
    )
    return N.render(raw, px, None, cache, levels)


# ----------------------------------------------------------------------
# The weather now, for a backdrop painted as it is
# ----------------------------------------------------------------------
TIMES = {
    "night": "a city street at night under street lamps, the vehicle's headlights on and its windows lit",
    "dawn": "the street at dawn, the first light low and the vehicle lit from the side",
    "morning": "the street on a bright morning, the light clean and from the side",
    "midday": "the street at midday, the light from above",
    "afternoon": "the street on a warm afternoon, the light long and from the side",
    "dusk": "the street at dusk, the last light on the vehicle and the first lamps lit",
}
WEATHERS = {
    "clear": "under a clear sky",
    "partly": "under scattered cloud",
    "overcast": "under a flat grey overcast",
    "fog": "in soft fog",
    "rain": "in the rain, the road wet and shining",
    "snow": "with snow falling",
    "storm": "under storm clouds",
}


def weather_bucket(code: int, cloud: float) -> str:
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (45, 48):
        return "fog"
    if code == 3 or cloud >= 80:
        return "overcast"
    if code in (1, 2) or cloud >= 25:
        return "partly"
    return "clear"


def time_bucket(mins: int, rise: int | None, set_: int | None) -> str:
    if rise is None or set_ is None:
        return "night" if mins < 6 * 60 or mins > 19 * 60 else "midday"
    if abs(mins - rise) <= 45:
        return "dawn"
    if abs(mins - set_) <= 45:
        return "dusk"
    if mins < rise or mins > set_:
        return "night"
    if mins < 11 * 60:
        return "morning"
    if mins < 14 * 60:
        return "midday"
    return "afternoon"


def _hhmm(text: str) -> int | None:
    t = str(text or "")[-5:]
    if len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit():
        return int(t[:2]) * 60 + int(t[3:])
    return None


def weather_now(
    lat: float, lon: float, mins: int, data_dir: Path, ua: str
) -> tuple[str, str]:
    cache = data_dir / "weather" / f"{lat:.2f}_{lon:.2f}.json"
    got = N.read_json(cache, None)
    if not (
        isinstance(got, dict) and time.time() - float(got.get("at") or 0) < WEATHER_S
    ):
        data, _err = N.get_json(OPEN_METEO.format(lat=lat, lon=lon), ua=ua, timeout=4)
        if isinstance(data, dict):
            got = {"at": time.time(), "data": data}
            N.write(cache, json.dumps(got).encode())
    data = (got or {}).get("data") if isinstance(got, dict) else None
    rise = set_ = None
    code, cloud = 0, 0.0
    if isinstance(data, dict):
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        with contextlib.suppress(TypeError, ValueError, IndexError):
            code = int(cur.get("weather_code") or 0)
            cloud = float(cur.get("cloud_cover") or 0)
            rise = _hhmm((daily.get("sunrise") or [""])[0])
            set_ = _hhmm((daily.get("sunset") or [""])[0])
    return time_bucket(mins, rise, set_), weather_bucket(code, cloud)


def weather_words(tod: str, wx: str) -> str:
    return f"{TIMES.get(tod, TIMES['midday'])}, {WEATHERS.get(wx, 'under a clear sky')}, in the same style"
