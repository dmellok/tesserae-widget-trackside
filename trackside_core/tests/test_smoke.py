"""Offline tests for the Trackside bundle: the feeds, the Melbourne fleet,
the photographs, the paintings and the four pages, with every call on the
network answered here."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

# The widget paints through Tesserae's ``app.artwork`` (0.463+): the server's
# repo must be importable. A checkout beside this one, or TESSERAE_REPO.
_TESSERAE = Path(
    os.environ.get("TESSERAE_REPO") or Path(__file__).resolve().parents[3] / "tesserae"
)
if str(_TESSERAE) not in sys.path:
    sys.path.insert(0, str(_TESSERAE))
from app import fal as fal_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lineup = _load(ROOT / "trackside_lineup" / "server.py", "trackside_lineup_server")
photo = _load(ROOT / "trackside_photo" / "server.py", "trackside_photo_server")
plate = _load(ROOT / "trackside_plate" / "server.py", "trackside_plate_server")
scene = _load(ROOT / "trackside_scene" / "server.py", "trackside_scene_server")
S = lineup._lib("sheet")
R = S.R
P = S.P
N = S.N
W = S.W

FLINDERS = {
    "latitude": -37.8183,
    "longitude": 144.9671,
    "location": {"label": "Flinders Street", "timezone": "Australia/Melbourne"},
    "source": "melbourne",
}
SETTINGS = {
    "vic_key": "vic-key",
    "fal_api_key": "k",
    "contact": "tester@example.com",
    "gen_daily_usd": "1",
}


# ----------------------------------------------------------------------
# A GTFS-Realtime feed, written here
# ----------------------------------------------------------------------
def _v(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _f(field: int, value) -> bytes:
    if isinstance(value, bytes):
        return _v(field << 3 | 2) + _v(len(value)) + value
    if isinstance(value, str):
        return _f(field, value.encode())
    if isinstance(value, float):
        return _v(field << 3 | 5) + struct.pack("<f", value)
    return _v(field << 3) + _v(value)


def feed(vehicles: list[dict]) -> bytes:
    now = int(time.time())
    body = _f(1, _f(1, "2.0") + _f(3, now))
    for i, v in enumerate(vehicles):
        pos = _f(1, float(v["lat"])) + _f(2, float(v["lon"]))
        if "bearing" in v:
            pos += _f(3, float(v["bearing"]))
        if "speed" in v:
            pos += _f(5, float(v["speed"]))
        trip = _f(1, v.get("trip", f"t{i}")) + _f(5, v.get("route", ""))
        vp = (
            _f(1, trip)
            + _f(2, pos)
            + _f(4, v.get("status", 2))
            + _f(5, v.get("ts", now))
            + _f(8, _f(1, v.get("id", "vid-" + v["label"])) + _f(2, v["label"]))
        )
        body += _f(2, _f(1, f"e{i}") + _f(4, vp))
    return body


TRAMS = [
    {
        "label": "6036",
        "route": "3-96-mjp-1",
        "lat": -37.8190,
        "lon": 144.9660,
        "bearing": 200.0,
        "speed": 6.0,
    },
    {
        "label": "3020",
        "route": "3-109-mjp-1",
        "lat": -37.8170,
        "lon": 144.9690,
        "status": 1,
    },
    {"label": "7015", "route": "3-58-mjp-1", "lat": -37.8150, "lon": 144.9640},
    {
        "label": "135",
        "route": "3-16-mjp-1",
        "lat": -37.9000,
        "lon": 145.1000,
    },  # far off
]
METRO = [
    {
        "label": "301M-1001T-302M",
        "route": "2-FKN-mjp-1",
        "lat": -37.8186,
        "lon": 144.9668,
        "status": 1,
    },
    {
        "label": "102M-1352T-101M",
        "route": "2-SDM-mjp-1",
        "lat": -37.8230,
        "lon": 144.9800,
        "speed": 15.0,
        "bearing": 120.0,
    },
]
VLINE = [{"label": "1105", "route": "1-BAT", "lat": -37.8180, "lon": 144.9530}]
COMMONS_FILES = {
    "E class trams": [
        "File:(AUS-Victoria) Yarra Trams No.6036 58 2025-12-06.jpg",
        "File:6001 interior.jpg",
    ],
    "Citadis 202 trams in Melbourne": [
        "File:C.3020 at Montague Street 1.jpg",
        "File:3001 at Box Hill.jpg",
    ],
    "G class trams": [
        "File:G-class tram at Moonee Ponds Junction 2026 1.jpg",
        "File:G-class tram mockup at Alstom warehouse.jpg",
    ],
    "Comeng (train)": [
        "File:Alstom Comeng 563M at Essendon 2024.jpg",
        "File:Multiple Comeng trains at the yard.jpg",
    ],
    "X'Trapolis 100 in Melbourne": ["File:102M Burnley.jpg"],
    "VLocity": ["File:Standard gauge VLocity at Broadmeadows station 2022.jpg"],
}


def _jpeg(w=600, h=400, blob=True, paper=(255, 255, 255)) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (w, h), paper)
    if blob:
        d = ImageDraw.Draw(im)
        d.rectangle((80, 170, 520, 260), fill=(40, 120, 60))
        d.rectangle((260, 140, 300, 170), fill=(60, 60, 70))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


@pytest.fixture
def offline(monkeypatch):
    """Every call on the network, answered here; the prompts fal.ai got and the feed requests."""
    prompts: list[dict] = []
    asked: list[dict] = []

    def fake_http(
        url,
        token="",
        accept="application/json",
        *,
        auth_scheme="Bearer",
        body=None,
        timeout=10,
        detail=False,
        ua="",
        headers=None,
    ):
        asked.append({"url": url, "headers": dict(headers or {}), "ua": ua})
        if "api.opendata.transport.vic.gov.au" in url:
            if (headers or {}).get("KeyID") != "vic-key":
                return None, "auth"
            which = TRAMS if "/tram/" in url else METRO if "/metro/" in url else VLINE
            return feed(which), None
        if "translink" in url:
            return feed(
                [{"label": "14", "route": "L1-5029", "lat": -28.0, "lon": 153.43}]
            ), None
        if "commons.wikimedia.org" in url:
            assert "tesserae-trackside" in ua and "tester@example.com" in ua
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if q.get("list") == "categorymembers":
                files = COMMONS_FILES.get(q["cmtitle"].split(":", 1)[1], [])
                return json.dumps(
                    {"query": {"categorymembers": [{"title": f} for f in files]}}
                ).encode(), None
            if q.get("list") == "search":
                return json.dumps({"query": {"search": []}}).encode(), None
            title = q.get("titles", "")
            lic = "CC BY-ND 4.0" if "Moonee Ponds" in title else "CC BY-SA 4.0"
            return json.dumps(
                {
                    "query": {
                        "pages": [
                            {
                                "title": title,
                                "imageinfo": [
                                    {
                                        "thumburl": "https://upload.wikimedia.org/"
                                        + urllib.parse.quote(title),
                                        "descriptionurl": "https://commons.wikimedia.org/wiki/"
                                        + urllib.parse.quote(title),
                                        "extmetadata": {
                                            "Artist": {
                                                "value": "<a href='x'>A. Spotter</a>"
                                            },
                                            "LicenseShortName": {"value": lic},
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            ).encode(), None
        if "upload.wikimedia.org" in url or "example.org" in url:
            return _jpeg(), None
        if "open-meteo" in url:
            return json.dumps(
                {
                    "current": {"weather_code": 61, "cloud_cover": 90, "is_day": 1},
                    "daily": {
                        "sunrise": ["2026-10-18T06:30"],
                        "sunset": ["2026-10-18T19:40"],
                    },
                }
            ).encode(), None
        return None, "unreachable"

    monkeypatch.setattr(N, "http", fake_http)

    def fake_fal(url, body, key, timeout):
        prompts.append({**body, "_url": url})
        assert key == "k"
        return {"images": [{"url": "https://fal.example/out.jpg"}]}

    monkeypatch.setattr(fal_mod, "_http_json", fake_fal)
    monkeypatch.setattr(
        fal_mod, "_download", lambda url, timeout=60.0: _jpeg(paper=(252, 250, 246))
    )
    yield prompts, asked
    P.ART.stop_all(Path("."))
    W.stop_watches()


def _busy() -> bool:
    pr = P.ART.progress(Path("."), {})
    return bool(pr["running"] or pr["queued"])


def _settle(timeout=10.0):
    end = time.time() + timeout
    while _busy() and time.time() < end:
        time.sleep(0.02)
    assert not _busy()


def _ctx(tmp_path, **kw):
    ctx = {"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 480, **kw}
    ctx.setdefault("settled", not ctx.get("preview"))
    return ctx


# ----------------------------------------------------------------------
def test_manifests_and_client_files():
    for wid in (
        "trackside_core",
        "trackside_photo",
        "trackside_plate",
        "trackside_lineup",
        "trackside_scene",
    ):
        m = json.loads((ROOT / wid / "plugin.json").read_text(encoding="utf-8"))
        assert (
            m["version"] == "0.1.0"
            and m["tesserae_compat"] == "1.x"
            and m["tesserae_min"] == "0.463.0"
        )
        if m["kind"] == "widget":
            assert (ROOT / wid / "client.js").read_text(encoding="utf-8").count(
                "/plugins/trackside_core/static/trackside.js"
            ) == 1
            names = [o["name"] for o in m["cell_options"]]
            assert (
                len(names) == len(set(names))
                and "source" in names
                and "period" in names
            )
    core = json.loads(
        (ROOT / "trackside_core" / "plugin.json").read_text(encoding="utf-8")
    )
    assert {s["name"] for s in core["settings"]} >= {
        "vic_key",
        "fal_api_key",
        "contact",
        "gen_daily_usd",
    }


def test_the_feed_decodes():
    rows = R.decode(feed(TRAMS[:1] + [{"label": "x", "lat": 0.0, "lon": 0.0}]), "tram")
    assert len(rows) == 1, "a position at 0,0 is no position"
    r = rows[0]
    assert r["label"] == "6036" and r["route"] == "3-96-mjp-1" and r["mode"] == "tram"
    assert (
        abs(r["lat"] + 37.819) < 1e-4
        and r["bearing"] == 200.0
        and r["speed_kmh"] == 21.6
        and r["status"] == "between stops"
    )


def test_the_melbourne_fleet_by_number():
    assert R.melbourne_vehicle("tram", "6036", "")["typ"] == "E-class tram"
    assert (
        R.melbourne_vehicle("tram", "3020", "")["cat"]
        == "Citadis 202 trams in Melbourne"
    )
    assert R.melbourne_vehicle("tram", "5110", "")["klass"] == "C2"
    assert R.melbourne_vehicle("tram", "4000", "")["typ"] == "Tram", (
        "a number no class has"
    )
    comeng = R.melbourne_vehicle("train", "301M-1001T-302M", "")
    assert (
        comeng["typ"] == "Comeng train"
        and comeng["num"] == "301M-1001T-302M"
        and comeng["car"] == "301M"
    )
    assert R.melbourne_vehicle("train", "102M", "")["typ"] == "X'Trapolis 100"
    assert R.melbourne_vehicle("train", "706M", "")["typ"] == "Siemens Nexas"
    assert (
        R.melbourne_vehicle("train", "9021", "")["typ"] == "High Capacity Metro Train"
    )
    assert R.melbourne_vehicle("regional", "1105", "")["op"] == "V/Line"
    assert (
        R.melbourne_route("tram", "3-96-mjp-1") == "Route 96"
        and R.melbourne_route("tram", "aus:vic:vic-03-96:") == "Route 96"
    )
    assert (
        R.melbourne_route("train", "2-FKN-mjp-1") == "Frankston line"
        and R.melbourne_route("train", "odd") == "odd"
    )


def test_photographs_by_number_and_by_class():
    files = [
        "File:(AUS-Victoria) Yarra Trams No.6036 58 2025-12-06.jpg",
        "File:6001 interior.jpg",
        "File:E.6038 arrives at South Melbourne 2026.jpg",
    ]
    assert R.pick_file(files, "6036") == (
        "File:(AUS-Victoria) Yarra Trams No.6036 58 2025-12-06.jpg",
        True,
    )
    assert R.pick_file(files, "6040") == (
        "File:E.6038 arrives at South Melbourne 2026.jpg",
        False,
    ), "her class, never an interior"
    assert R.pick_file(["File:706 at South Yarra.jpg"], "706M", "706M") == (
        "File:706 at South Yarra.jpg",
        True,
    )
    assert (
        R.pick_file(["File:Gold Coast Light Rail Stage 2 2017 2.jpg"], "2")[1] is False
    ), "a stray small number is no fleet number"
    assert R.pick_file(["File:G:link tram No. 2 at Broadbeach.jpg"], "2")[1] is True
    assert (
        R.pick_file(
            [
                "File:Multiple Comeng trains at the yard.jpg",
                "File:Comeng 563M at Essendon 2024.jpg",
            ],
            "999M",
        )[0]
        == "File:Comeng 563M at Essendon 2024.jpg"
    ), "one vehicle, not a crowd"


def test_melbourne_needs_the_key_and_sends_it(tmp_path, offline):
    _prompts, asked = offline
    rows, err, _feed = R.nearby(
        "melbourne", {}, "", -37.8183, 144.9671, 3, tmp_path, "ua"
    )
    assert rows == [] and "Transport Victoria open data key" in err
    rows, err, feed_name = R.nearby(
        "melbourne", {}, "vic-key", -37.8183, 144.9671, 3, tmp_path, "ua"
    )
    assert err is None and feed_name == "Transport Victoria"
    assert {
        a["headers"].get("KeyID") for a in asked if "transport.vic" in a["url"]
    } == {"vic-key"}
    assert [r["num"] for r in rows] == [
        "301M-1001T-302M",
        "6036",
        "3020",
        "7015",
        "102M-1352T-101M",
        "1105",
    ], "nearest first; the far tram left out"
    tram = next(r for r in rows if r["num"] == "6036")
    assert (
        tram["line"] == "Route 96"
        and tram["typ"] == "E-class tram"
        and tram["trk"] == 200
        and tram["kmh"] == 21.6
    )
    assert (
        next(r for r in rows if r["num"].startswith("301M"))["line"] == "Frankston line"
    )
    # kept thirty seconds: a second look asks nothing
    n = len(asked)
    R.nearby("melbourne", {}, "vic-key", -37.8183, 144.9671, 3, tmp_path, "ua")
    assert len(asked) == n
    # a refused key says so
    rows, err, _ = R.nearby(
        "melbourne", {}, "wrong", -37.8183, 144.9671, 3, tmp_path / "other", "ua"
    )
    assert rows == [] and "refused the key" in err


def test_lineup_with_photographs(tmp_path, offline):
    r = lineup.fetch(
        {**FLINDERS, "pictures": "photos", "labels": ["type", "line", "distance"]},
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    assert (
        r["state"] == "ok"
        and r["within"] == 6
        and r["attribution"] == "positions from Transport Victoria"
    )
    by = {v["num"]: v for v in r["vehicles"]}
    assert (
        by["6036"]["pic"]["kind"] == "photo" and not by["6036"]["pic"]["impression"]
    ), "her own photograph"
    assert by["7015"]["pic"] is None, (
        "the G-class photograph is ND-licensed and is never used"
    )
    assert (
        by["301M-1001T-302M"]["pic"]["impression"]
        and "a vehicle of her class" in by["301M-1001T-302M"]["pic"]["credit"]
    )
    assert by["6036"]["lines"] == ["E-class tram", "Route 96", "0.1 km SW"]
    assert by["6036"]["brand"] == {}, "the line-up is in paper and ink"
    r = lineup.fetch(
        {**FLINDERS, "pictures": "photos", "modes": ["tram"]},
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    assert {v["cls"] for v in r["vehicles"]} == {"tram"}


def test_photo_plate_and_scene_pages(tmp_path, offline):
    r = photo.fetch({**FLINDERS}, SETTINGS, ctx=_ctx(tmp_path))
    assert (
        r["state"] == "ok"
        and r["vehicles"][0]["num"] == "301M-1001T-302M"
        and len(r["vehicles"]) == 5
    )
    r = plate.fetch(
        {**FLINDERS, "pictures": "photos", "look": "tiles", "modes": ["tram"]},
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    v = r["vehicles"][0]
    assert (
        v["num"] == "6036"
        and v["brand"]["primary"]
        and v["air"] == "Yarra Trams"
        and v["reg"] == "Route 96"
    )
    assert (
        v["about"].startswith("No. 6036 is a Bombardier Flexity")
        and "Route 96" in v["about"]
    )
    r = scene.fetch(
        {**FLINDERS, "modes": ["tram"]},
        {**SETTINGS, "fal_api_key": ""},
        ctx=_ctx(tmp_path),
    )
    assert (
        r["state"] == "ok"
        and r["backdrop"] == "street"
        and r["vehicles"][0]["pic"]["kind"] == "photo"
    )


def test_paintings_from_her_own_photograph_and_from_her_class(tmp_path, offline):
    prompts, _asked = offline
    opts = {
        **FLINDERS,
        "pictures": "paintings",
        "style": "linocut",
        "modes": ["tram", "train"],
        "count": "5",
    }
    r = lineup.fetch(opts, SETTINGS, ctx=_ctx(tmp_path))
    _settle()
    assert len(prompts) == 4, (
        "6036 and 3020 from their own, the Comeng and the X'Trapolis..."
    )
    own = [p for p in prompts if "number 6036" in p["prompt"]]
    assert len(own) == 1 and "another tram of the same class" not in own[0]["prompt"]
    assert all(
        "linocut" in p["prompt"] and p["image_urls"][0].startswith("data:image/")
        for p in prompts
    )
    cls = [p for p in prompts if "another train of the same class" in p["prompt"]]
    assert len(cls) == 1, "the Comeng from a photograph of her class"
    r = lineup.fetch(opts, SETTINGS, ctx=_ctx(tmp_path))
    by = {v["num"]: v for v in r["vehicles"]}
    assert (
        by["6036"]["pic"]["kind"] == "painting" and not by["6036"]["pic"]["impression"]
    )
    assert (
        by["301M-1001T-302M"]["pic"]["impression"]
        and "a photograph of her class" in by["301M-1001T-302M"]["pic"]["credit"]
    )
    assert any("without a photograph to paint from" in s for s in r["status"]), (
        "the G-class"
    )
    assert len(prompts) == 4, "painted once"


def test_only_vehicles_with_their_own_photograph_are_painted(tmp_path, offline):
    prompts, _asked = offline
    r = lineup.fetch(
        {
            **FLINDERS,
            "pictures": "paintings",
            "paint": "own",
            "modes": ["tram", "train"],
            "count": "5",
        },
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    _settle()
    assert len(prompts) == 3 and not any(
        "The photograph is of another" in p["prompt"] for p in prompts
    ), "6036, 3020 and 102M"
    assert any("left unpainted" in s for s in r["status"])


def test_everything_since_the_last_render(tmp_path, offline):
    opts = {
        **FLINDERS,
        "pictures": "photos",
        "period": "since",
        "modes": ["tram"],
        "labels": ["type", "distance", "seen"],
    }
    r = lineup.fetch(opts, SETTINGS, ctx=_ctx(tmp_path))
    assert r["period"].startswith("since ") and r["status"][0].startswith(
        "3 seen since "
    )
    assert r["vehicles"][0]["lines"][1].startswith("closest ") and r["vehicles"][0][
        "lines"
    ][2].startswith("within reach since ")
    gone = TRAMS.pop(0)
    try:
        for f in (tmp_path / "live").glob("*.json"):
            f.unlink()
        r = lineup.fetch(opts, SETTINGS, ctx=_ctx(tmp_path))
        assert "6036" in [v["num"] for v in r["vehicles"]], (
            "seen since the window began, still on the page"
        )
        for f in (tmp_path / "live").glob("*.json"):
            f.unlink()
        r = lineup.fetch({**opts, "period": "now"}, SETTINGS, ctx=_ctx(tmp_path))
        assert "6036" not in [v["num"] for v in r["vehicles"]]
    finally:
        TRAMS.insert(0, gone)


def test_errors_are_words(tmp_path, offline):
    r = lineup.fetch({**FLINDERS}, {**SETTINGS, "vic_key": ""}, ctx=_ctx(tmp_path))
    assert r["state"] == "error" and "Transport Victoria open data key" in r["error"]
    r = lineup.fetch({"source": "melbourne"}, SETTINGS, ctx=_ctx(tmp_path))
    assert r["state"] == "error" and "Set a place" in r["error"]
    r = lineup.fetch({**FLINDERS, "source": "custom"}, SETTINGS, ctx=_ctx(tmp_path))
    assert r["state"] == "error" and "GTFS-Realtime" in r["error"]
    r = lineup.fetch(
        {**FLINDERS, "radius_km": "1", "latitude": -36.0}, SETTINGS, ctx=_ctx(tmp_path)
    )
    assert r["state"] == "empty"


def test_another_agencys_feed(tmp_path, offline):
    _prompts, asked = offline
    opts = {
        **FLINDERS,
        "source": "custom",
        "feed_urls": "https://gtfsrt.api.translink.com.au/x/Tram\nnot a link",
        "key_header": "x-api-key",
        "feed_key": "abc",
        "operator": "G:link",
        "latitude": -28.0,
        "longitude": 153.43,
        "pictures": "photos",
    }
    r = lineup.fetch(opts, SETTINGS, ctx=_ctx(tmp_path))
    assert (
        r["state"] == "ok"
        and r["vehicles"][0]["num"] == "14"
        and r["attribution"] == "positions from G:link"
    )
    assert [a["headers"] for a in asked if "translink" in a["url"]] == [
        {"x-api-key": "abc"}
    ]


def test_side_views_for_the_painter(tmp_path, offline):
    prompts, _asked = offline
    assert (
        R.side_key({"klass": "Z3"}) == "Z"
        and R.side_key({"klass": "E2"}) == "E"
        and R.side_key({"klass": "comeng"}) == "comeng"
    )
    # the line-up paints side-on from both pictures by default
    r = lineup.fetch(
        {
            **FLINDERS,
            "pictures": "paintings",
            "style": "linocut",
            "modes": ["tram"],
            "count": "2",
        },
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    _settle()
    assert len(prompts) == 2 and all(len(p["image_urls"]) == 2 for p in prompts), (
        "her photograph and her class's side view"
    )
    assert all(
        "pure side elevation" in p["prompt"]
        and "second picture shows a tram of the same class from the side" in p["prompt"]
        for p in prompts
    )
    r = lineup.fetch(
        {
            **FLINDERS,
            "pictures": "paintings",
            "style": "linocut",
            "modes": ["tram"],
            "count": "2",
        },
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    pic = r["vehicles"][0]["pic"]
    assert (
        pic["kind"] == "painting"
        and "and a side view by A. Spotter (CC BY-SA 4.0)" in pic["credit"]
    )
    # the plate keeps the photograph's angle and takes the side view for her length
    prompts.clear()
    plate.fetch(
        {**FLINDERS, "pictures": "paintings", "style": "linocut", "modes": ["tram"]},
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    _settle()
    assert (
        len(prompts) == 1
        and "faithful translation" in prompts[0]["prompt"]
        and "take her length, her sections" in prompts[0]["prompt"]
    )
    # a side view of your own comes first; FLUX Kontext takes one picture only
    prompts.clear()
    own = {**SETTINGS, "side_views": "E = https://example.org/e-side.jpg\nnot a line"}
    lineup.fetch(
        {
            **FLINDERS,
            "pictures": "paintings",
            "style": "riso",
            "modes": ["tram"],
            "count": "1",
        },
        own,
        ctx=_ctx(tmp_path),
    )
    _settle()
    assert len(prompts) == 1 and len(prompts[0]["image_urls"]) == 2
    assert R.own_side_views("E = https://example.org/e-side.jpg\nnot a line") == {
        "e": "https://example.org/e-side.jpg"
    }
    prompts.clear()
    lineup.fetch(
        {
            **FLINDERS,
            "pictures": "paintings",
            "style": "pixel",
            "model": "flux_kontext",
            "modes": ["tram"],
            "count": "1",
        },
        SETTINGS,
        ctx=_ctx(tmp_path),
    )
    _settle()
    assert (
        len(prompts) == 1
        and "image_url" in prompts[0]
        and "image_urls" not in prompts[0]
    )
