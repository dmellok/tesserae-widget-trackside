"""Write the four widgets' plugin.json and the core's from one description,
so the shared options never drift apart. Run after editing: python tools/manifests.py"""

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

import importlib.util as _ilu
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.5.1"
TESSERAE_MIN = "0.463.0"


def _load(name):
    spec = _ilu.spec_from_file_location(
        f"_tesserae_plugins.trackside_core.{name}",
        ROOT / "trackside_core" / "lib" / f"{name}.py",
    )
    mod = _ilu.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_paint = _load("paint")
_rt = _load("rt")
# each painted style shows its sample (tools/style_previews.py) in the picker
STYLES = [
    (k, v[0], f"/plugins/trackside_core/static/styles/{k}.jpg")
    if (ROOT / "trackside_core" / "static" / "styles" / f"{k}.jpg").exists()
    else (k, v[0])
    for k, v in _paint.STYLES.items()
    if k not in _paint.HIDDEN
] + [
    ("builder", "Build my own"),
    ("custom", "Your own prompt"),
]
BUILDER = {
    name: (label, default, [(v, lab) for v, lab, _p in choices])
    for name, (label, default, choices) in _paint.BUILDER.items()
}
PRESETS = [
    {"label": p["label"], "value": p["value"].replace("{kind}", "tram")}
    for p in _paint.CUSTOM_PRESETS
]
BACKDROPS = [(k, v[0]) for k, v in _paint.BACKDROPS.items()]
MODELS = [(k, f"{v['label']} ({v['cost']})") for k, v in _paint.MODELS.items()]


def sel(name, label, default, choices, **kw):
    return {
        "name": name,
        "type": "select",
        "label": label,
        "default": default,
        "choices": [
            {"value": c[0], "label": c[1], **({"image": c[2]} if len(c) > 2 else {}), **({"image_large": c[2].replace("/styles/", "/styles/large/")} if len(c) > 2 else {})}
            for c in choices
        ],
        **kw,
    }


def adv(option):
    """Fine-tuning: Tesserae folds these into a collapsed More options at the end of the panel."""
    return {**option, "advanced": True}


# The panel reads as Fugleramme's: where the positions come from, the place and the reach, how
# many, the pictures and the paper up front; what one choice needs nested under it; the
# fine-tuning under More options.


def where_options():
    custom = {"source": "custom"}
    return [
        sel(
            "source",
            "Positions from",
            "melbourne",
            [
                (
                    "ptv",
                    "Melbourne: PTV Timetable API (developer id and key): trains live with their destination, trams due at the stops near you",
                )
            ]
            + [(k, p["label"]) for k, p in _rt.PRESETS.items()]
            + [("custom", "Another agency's GTFS-Realtime feed")],
            help="Melbourne two ways. The PTV Timetable API (a developer id and key from PTV) gives every Metro train's live position, cars and destination, and each tram by fleet number at the stop it is due at next, with the minutes to go; it has no live V/Line. Transport Victoria's open data (a different, free key) gives every train and tram's live position but no destinations. The others need no key. Another agency: most publish their vehicle positions as GTFS-Realtime; give the feed's link below.",
        ),
        {
            "name": "feed_urls",
            "type": "textarea",
            "label": "Vehicle positions feeds",
            "default": "",
            "show_if": custom,
            "help": "One GTFS-Realtime VehiclePositions link a line, up to four (the agency's open data page names them).",
        },
        {
            "name": "key_header",
            "type": "string",
            "label": "Key header",
            "default": "",
            "show_if": custom,
            "help": "The HTTP header the agency wants its key in, for example x-api-key or Authorization. Blank for a feed without a key.",
        },
        {
            "name": "feed_key",
            "type": "string",
            "label": "Key",
            "default": "",
            "secret": True,
            "show_if": custom,
        },
        {
            "name": "operator",
            "type": "string",
            "label": "Operator, for the photographs",
            "default": "",
            "show_if": custom,
            "help": "The name Wikimedia Commons knows the operator by (for example Sydney Trains); the widget looks for a photograph of each vehicle by this and her number.",
        },
        {
            "name": "location",
            "type": "location_search",
            "label": "Place",
            "default": "",
            "help": "The point the distances and bearings are from. Falls back to the app's location.",
        },
        sel(
            "radius_km",
            "Within",
            "3",
            [(v, f"{v} km") for v in ("1", "2", "3", "5", "10", "15", "25")],
        ),
    ]


def traffic_options():
    return [
        adv(o)
        for o in [
            {
                "name": "modes",
                "type": "multiselect",
                "label": "Show",
                "default": ["tram", "train", "regional", "subway"],
                "choices": [
                    {"value": "tram", "label": "Trams and light rail"},
                    {"value": "train", "label": "Suburban trains"},
                    {"value": "regional", "label": "Regional trains"},
                    {"value": "subway", "label": "Subway and metro"},
                ],
            },
            sel(
                "order",
                "Order",
                "nearest",
                [
                    ("nearest", "Nearest first"),
                    ("fastest", "Fastest first"),
                    ("longest", "Longest first"),
                ],
            ),
            sel(
                "period",
                "When",
                "now",
                [
                    ("now", "What is within reach now"),
                    ("since", "Everything that passed since the last render"),
                ],
                help="Since the last render keeps a watch on the place between renders (the feeds asked every minute) and shows every train and tram seen since the page was last drawn for a panel, each at her closest approach with the time. The editor's previews show the same window without moving it on; the watch stops three hours after a page last asked, and a first render shows the last hour.",
            ),
        ]
    ]


def picture_options(with_backdrop, many, scene=False):
    show = {"pictures": "paintings"}
    out = [
        sel(
            "pictures",
            "Pictures",
            "photos",
            [
                ("photos", "Photographs (Wikimedia Commons)"),
                ("paintings", "AI paintings, each from a photograph"),
            ],
            help="Photographs cost nothing and need no key: Wikimedia Commons by the vehicle's number in her class's category, or a photograph of her class where Commons has none of her own (marked so). AI paintings paint each vehicle once from that photograph, in the style below, at about US$0.04 a painting; needs a fal.ai key.",
        ),
        sel("style", "Style", "poster", STYLES, show_if=show),
        {
            "name": "custom_style",
            "type": "textarea",
            "label": "Your own prompt",
            "default": "",
            "show_if": {"pictures": "paintings", "style": "custom"},
            "presets": PRESETS,
            "help": "The medium and the look; the widget adds the vehicle, her photograph and the paper. Pick an example above to start from.",
        },
    ]
    for name, (label, default, choices) in BUILDER.items():
        out.append(
            sel(
                f"build_{name}",
                label,
                default,
                choices,
                show_if={"pictures": "paintings", "style": "builder"},
            )
        )
    out.append(
        {
            "name": "build_extra",
            "type": "string",
            "label": "Your own words",
            "default": "",
            "show_if": {"pictures": "paintings", "style": "builder"},
            "help": "Added to the style: for example, with a little gold leaf on the pantograph.",
        }
    )
    if with_backdrop:
        out.append(
            sel(
                "backdrop",
                "Backdrop",
                "street" if scene else "none",
                BACKDROPS if not scene else BACKDROPS[1:],
                show_if=show,
                help="None keeps the vehicle alone on the page, cut out. The rest paint a whole picture that fills its frame, in the same style. The street as it is now takes the time of day and the weather (Open-Meteo) at the place, and paints each condition once per vehicle, so a changing sky costs more paintings.",
            )
        )
    return out


def painting_options(many):
    show = {"pictures": "paintings"}
    out = []
    out.append(
            sel(
                "paint",
                "Paint",
                "all",
                [
                    ("all", "Every vehicle with a photograph, her own or her class's"),
                    ("own", "Only vehicles with a photograph of their own"),
                ],
                show_if=show,
                help="A painting from a photograph of her class keeps the class's shape and the operator's standard livery but not her own wrap or number, and is marked an impression. Only vehicles with a photograph of their own asks for no such painting; the rest show as the setting below says.",
            )
    )
    out.append(
        sel(
            "fidelity",
            "Faithfulness",
            "side" if many else "photo",
            [
                ("photo", "As photographed: the same angle, every mark of the livery"),
                (
                    "side",
                    "Side-on, from both pictures: her livery from her photograph, her length and doors from her class's side view",
                ),
                ("free", "Drawn afresh: the model's own composition, the view below"),
            ],
            show_if=show,
            help="Most photographs of a tram or a train are taken from the front three-quarter. Side-on hands the painter a second picture of her class seen from the side (chosen by hand for each Melbourne class, or your own under the Trackside Core settings) and paints a pure side elevation, the way a fleet line-up reads; a class without a side view is painted side-on from her photograph alone. The other two use the side view too, where there is one, for her length and the spacing of her doors.",
        )
    )
    out.append(
        sel(
            "view",
            "View",
            "side",
            [
                (
                    k,
                    {
                        "side": "From the side, a little to the front",
                        "profile": "Side-on: a pure profile, as a builder's drawing",
                        "quarter": "From the front three-quarter, coming on",
                        "head": "Head-on",
                    }[k],
                )
                for k in _paint.VIEWS
            ],
            show_if={"pictures": "paintings", "fidelity": "free"},
        )
    )
    out.append(
        sel(
            "model",
            "Model",
            "nano_banana",
            MODELS,
            show_if=show,
            help="All on fal.ai, all painting from the photograph. On Nano Banana, pixel art, stained glass, low-poly, paper craft, toy bricks and the grey styles (engraving, pencil, charcoal) are painted with GPT Image 2 (about US$0.05), as Nano Banana keeps them close to the photograph.",
        )
    )
    if many:
        out.append(
            sel(
                "unpainted",
                "Until a vehicle is painted",
                "silhouette",
                [
                    ("silhouette", "Show a silhouette"),
                    ("photo", "Show her photograph"),
                    ("skip", "Only painted vehicles: leave the rest off the page"),
                ],
                show_if=show,
            )
        )
        out.append(
            sel(
                "when_spent",
                "When the day's budget is spent",
                "keep",
                [
                    ("keep", "Keep showing what is on the rails"),
                    ("random", "Fill the gaps with paintings already kept, at random"),
                    ("rare", "Fill the gaps with the rarest ones seen"),
                ],
                show_if=show,
                help="Once to-day's painting budget is spent, the trains and trams that could not be painted give way to paintings of other vehicles this widget already kept in the same style: a different handful each hour, or the ones seen fewest times first. Their labels say when each was last seen. Only paintings from a vehicle's own photograph are used, not those of her class, and nothing is spent on them.",
            )
        )
    else:
        out.append(
            sel(
                "unpainted",
                "Until the vehicle is painted",
                "photo",
                [
                    ("photo", "Show her photograph"),
                    ("silhouette", "Show a silhouette"),
                    ("skip", "Only a painted vehicle: the nearest one already painted"),
                ],
                show_if=show,
            )
        )
    return [adv(o) for o in out]


def paper_options(**kw):
    return [
        sel(
            "paper",
            "Paper",
            "cream",
            [
                ("cream", "Cream"),
                ("white", "White"),
                ("aged", "Aged"),
                ("blueprint", "Blueprint: pale ink on blue"),
                ("sky", "A soft sky wash"),
            ],
            **kw,
        ),
    ]


def page_options(labels_default):
    return [adv(o) for o in [
        {
            "name": "labels",
            "type": "multiselect",
            "label": "Label lines",
            "default": labels_default,
            "help": "In this order, under or beside each vehicle.",
            "choices": [
                {"value": v, "label": l}
                for v, l in [
                    ("type", "Class"),
                    ("line", "Line or route"),
                    ("dest", "Where she is going"),
                    ("number", "Fleet number"),
                    ("operator", "Operator"),
                    ("status", "At a stop or between"),
                    ("speed", "Speed"),
                    ("distance", "Distance and bearing"),
                    ("heading", "Heading"),
                    ("seen", "When she was within reach"),
                ]
            ],
        },
        {
            "name": "title",
            "type": "string",
            "label": "Title",
            "default": "",
            "help": "Blank uses the place.",
        },
        sel(
            "header",
            "Title line",
            "top",
            [
                ("top", "Above the page"),
                ("bottom", "Below the page"),
                ("hidden", "Hidden"),
            ],
        ),
        sel(
            "footer",
            "Foot of the page",
            "show",
            [
                ("show", "Status and credits"),
                ("credits", "Credits only"),
                ("hide", "Nothing"),
            ],
        ),
        sel(
            "panel",
            "Panel",
            "colour",
            [
                ("colour", "Colour"),
                ("gray16", "16 greys"),
                ("gray4", "4 greys"),
                ("mono", "Black and white"),
            ],
            help="What the panel can show. Pictures are converted and contrast-stretched to match.",
        ),
        sel(
            "text_size",
            "Text size",
            "100",
            [(v, f"{v}%") for v in ("60", "80", "100", "125", "150")],
        ),
        sel(
            "font",
            "Typeface",
            "fell",
            [
                ("fell", "IM Fell English: the old plate"),
                ("archivo", "Archivo Narrow: clean and plain"),
                ("georgia", "Georgia: a modern serif"),
                ("page", "The dashboard's own font"),
            ],
        ),
    ]]


WIDGETS = {
    "trackside_photo": {
        "name": "Trackside: Photo",
        "icon": "ph-train",
        "description": "The nearest train or tram to a place, right now, as a full-bleed photograph of the very vehicle or her class (Wikimedia Commons), with her number, class, line and distance, and the next few along a strip. Melbourne with a free key, or any agency's GTFS-Realtime feed.",
        "options": lambda: (
            where_options()
            + [
                {
                    "name": "strip",
                    "type": "number",
                    "label": "How many along the strip",
                    "default": 4,
                    "help": "0 to 8 after the nearest.",
                },
            ]
            + paper_options()
            + traffic_options()
            + [
                adv(
                    sel(
                        "photo_fit",
                        "Photograph fit",
                        "cover",
                        [("cover", "Fills the page"), ("contain", "Fits inside the page")],
                    )
                ),
            ]
            + page_options(["line", "speed", "distance"])
        ),
    },
    "trackside_plate": {
        "name": "Trackside: Plate",
        "icon": "ph-frame-corners",
        "description": "One train or tram near you as a plate: painted from her photograph in a style of your choosing, in a ruled frame, with her particulars beside her; or one of four modern looks in her operator's colours.",
        "options": lambda: (
            where_options()
            + picture_options(True, False)
            + [
                sel(
                    "look",
                    "Look",
                    "plate",
                    [
                        (
                            "plate",
                            "The plate: a ruled frame, the particulars beside it",
                        ),
                        ("pass", "Ticket: the picture, a panel of particulars"),
                        ("tiles", "Tiles: a bar, the picture, a tile for each figure"),
                        (
                            "poster",
                            "Poster: the picture full-bleed, the particulars over it",
                        ),
                        ("split", "Split: the picture framed on a dark page"),
                    ],
                ),
            ]
            + paper_options(show_if={"look": "plate"})
            + traffic_options()
            + painting_options(False)
            + [
                adv(
                    sel(
                        "colours",
                        "Colours",
                        "operator",
                        [
                            (
                                "operator",
                                "The operator's own: Yarra Trams green, Metro blue, V/Line purple",
                            ),
                            ("paper", "Paper and ink"),
                        ],
                    )
                ),
            ]
            + page_options(["line", "status", "distance"])
        ),
    },
    "trackside_lineup": {
        "name": "Trackside: Line-up",
        "icon": "ph-tram",
        "description": "Every train and tram near a place on one page: each painted from her photograph and cut out, sized by her length, with a caption; or, with photographs, a spotter's log of cards. Twenty-six styles, a style builder and your own prompt.",
        "options": lambda: (
            where_options()
            + [
                {
                    "name": "count",
                    "type": "number",
                    "label": "How many",
                    "default": 6,
                    "help": "1 to 12.",
                }
            ]
            + picture_options(False, True)
            + paper_options()
            + traffic_options()
            + painting_options(True)
            + [
                adv({
                    "name": "scale",
                    "type": "slider",
                    "label": "Scale",
                    "default": 100,
                    "min": 40,
                    "max": 100,
                    "step": 5,
                    "unit": "%",
                    "show_if": {"pictures": "paintings"},
                }),
            ]
            + page_options(["type", "line", "distance"])
        ),
    },
    "trackside_scene": {
        "name": "Trackside: Scene",
        "icon": "ph-buildings",
        "description": "The nearest train or tram as one whole painting across the page: the vehicle from her photograph in a painted street, at the platform, over a bridge, through the country, at night or in the rain, with her particulars set over it.",
        "options": lambda: (
            where_options()
            + picture_options(True, False, scene=True)
            + paper_options()
            + traffic_options()
            + painting_options(False)
            + page_options(["line", "status", "distance"])
        ),
    },
}

CORE = {
    "tesserae_compat": "1.x",
    "tesserae_min": TESSERAE_MIN,
    "name": "Trackside Core",
    "version": VERSION,
    "kind": "data",
    "description": "The settings and the paintings page for the Trackside widgets (trackside_photo, trackside_plate, trackside_lineup, trackside_scene): the Transport Victoria key, the fal.ai key, the contact for Wikimedia Commons and the daily painting budget. No widget of its own.",
    "icon": "ph-train",
    "supports": {"sizes": []},
    "requires": ["network:*"],
    "settings": [
        {
            "name": "ptv_devid",
            "type": "string",
            "label": "PTV Timetable API developer id",
            "default": "",
            "help": "For the PTV Timetable API source: the developer id PTV sent with your key (ask for one by email to APIKeyRequest@ptv.vic.gov.au, as their developer page says). Not needed for the open data source.",
        },
        {
            "name": "ptv_key",
            "type": "string",
            "label": "PTV Timetable API key",
            "secret": True,
            "default": "",
            "help": "The key that goes with the developer id. It never leaves this server: each request is signed with it, and only the signature is sent to timetableapi.ptv.vic.gov.au.",
        },
        {
            "name": "vic_key",
            "type": "string",
            "label": "Transport Victoria open data key",
            "secret": True,
            "default": "",
            "help": "For Melbourne's trains and trams. Free: sign up at https://opendata.transport.vic.gov.au, then make an API key under your account. It is sent only to api.opendata.transport.vic.gov.au, in the KeyID header. Not needed for the other places or a feed of your own.",
        },
        {
            "name": "fal_api_key",
            "type": "string",
            "label": "fal.ai API key (AI paintings)",
            "secret": True,
            "default": "",
            "help": "With a key the widgets can paint each vehicle from her photograph, at about US$0.04 a painting, once per vehicle and style. Leave it blank to use the key under Settings › Generative AI. Without either the widgets show photographs.",
        },
        {
            "name": "side_views",
            "type": "textarea",
            "label": "Side views of your own",
            "default": "",
            "help": "Pictures of a class seen from the side, for the painter, one a line: class = link. The class as the page names it (E-class tram) or the widget's short name (E, C2, D1, comeng, nexas, xtrap, hcmt, vlocity), or another agency's class as its page shows it. Yours come before the ones the widget carries for Melbourne.",
        },
        {
            "name": "contact",
            "type": "string",
            "label": "Contact for Wikimedia Commons",
            "default": "",
            "help": "An email address or a web page, sent in the widget's User-Agent as Wikimedia asks of every application that reads its API. The widget names its own project page by default.",
        },
        {
            "name": "gen_daily_usd",
            "type": "slider",
            "label": "Painting budget per day",
            "default": 4,
            "min": 0,
            "max": 30,
            "step": 1,
            "unit": "US$",
            "help": "The most the widgets may spend on new paintings in a day, at the models' listed prices. 0 is no limit, within the ceiling under Settings › Generative AI. Paintings are kept, so a vehicle in a style only costs once; a class painted from a class photograph is shared by every vehicle of that class.",
        },
        {
            "name": "gen_per_refresh",
            "type": "number",
            "label": "New paintings started per preview",
            "default": 3,
            "help": "How many new paintings one settled preview in the editor may start (1 to 50). A panel's own render starts what its page needs, within the daily budget.",
        },
        {
            "name": "gen_parallel",
            "type": "number",
            "label": "Paintings made at once",
            "default": 3,
            "help": "How many paintings fal.ai works on side by side (1 to 8).",
        },
    ],
}

for wid, w in WIDGETS.items():
    manifest = {
        "tesserae_compat": "1.x",
        "name": w["name"],
        "version": VERSION,
        "kind": "widget",
        "tesserae_min": TESSERAE_MIN,
        "description": w["description"],
        "icon": w["icon"],
        "supports": {"sizes": ["sm", "md", "lg"]},
        "requires": [
            "network:*",
            "settings:plugin/trackside_core",
            "plugin:trackside_core",
        ],
        "render": {"needs_network": True, "warm_ahead_s": 150},
        "design": {"palette": "extended"},
        "cell_options": w["options"](),
        "data_schema": {
            "fields": [
                {"name": "title", "type": "str", "label": "Title"},
                {
                    "name": "within",
                    "type": "num",
                    "label": "Trains and trams within reach",
                },
                {"name": "vehicles", "type": "arr", "label": "Trains and trams shown"},
            ]
        },
    }
    names = [o["name"] for o in manifest["cell_options"]]
    assert len(names) == len(set(names)), (
        wid,
        [n for n in names if names.count(n) > 1],
    )
    (ROOT / wid / "plugin.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(wid, len(names), "options")
(ROOT / "trackside_core" / "plugin.json").write_text(
    json.dumps(CORE, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
print("trackside_core", len(CORE["settings"]), "settings")
