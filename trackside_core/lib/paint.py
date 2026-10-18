"""AI paintings of trains and trams through fal.ai: each vehicle painted once
from a photograph of her (or of her class), in a style, on white paper (cut
out on the page) or as a whole picture on a painted backdrop. The prompts, the styles and the cut-out
are here; the budget, the queue, the files and the admin page are Tesserae's
(``app.artwork``, 0.463 and later). Loaded by path."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import re
import sys
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

try:
    from app import artwork  # Tesserae 0.463+: the pictures a widget makes and keeps
    from app import fal as _fal
except (
    ImportError
):  # an older Tesserae: the widgets still draw, from photographs, and say why
    artwork = None  # type: ignore[assignment]
    _fal = None  # type: ignore[assignment]

NEEDS_HOST = "AI paintings need Tesserae 0.463 or later; this server is older, so the photographs show."


class _NoArt:
    """What ART.open() hands a fetch on a Tesserae without app.artwork: every
    request answers no_host, nothing is started, the note says why."""

    key = ""
    preview = False
    settled = True
    subject = ""
    cell = ""
    cap = 0
    started = 0
    pending = 0
    busy = False
    c = None

    def __init__(self) -> None:
        self.states: dict[str, int] = {}
        self.asked: dict[str, Any] = {}
        self.data_dir = Path(".")

    def request(
        self, key: str, payload: dict[str, Any], *, title: str = "", want: bool = True
    ) -> Any:
        from types import SimpleNamespace

        self.states["no_host"] = self.states.get("no_host", 0) + 1
        return SimpleNamespace(
            key=key,
            state="no_host",
            ready=False,
            path=None,
            meta={},
            why=NEEDS_HOST,
            credit="",
        )

    def url(self, *a: Any, **k: Any) -> str:
        return ""

    def picture(self, *a: Any, **k: Any) -> None:
        return None

    def live(self) -> dict[str, int]:
        return {"live_refresh_s": 0, "live_pending": 0}

    def notes(self) -> list[str]:
        return [NEEDS_HOST] if self.states.get("no_host") else []

    def status(self) -> list[str]:
        return []

    def finish(self, *a: Any, **k: Any) -> None:
        return None


class _NoArtwork:
    """The collection on an older Tesserae: it opens to _NoArt and answers
    nothing to everything else (the paintings page does not exist there)."""

    name = "paintings"
    href = "/plugins/trackside_core/"
    actions: list[Any] = []
    extra_template = None
    extra_context = None
    _DEFAULTS: dict[str, Any] = {
        "state": "none",
        "why_failed": "",
        "held": {},
        "dashboards": [],
        "listing": [],
        "errors": [],
        "versions": [],
        "budget_used": 0,
        "cap": 0,
        "adopt": False,
        "progress": {
            "running": [],
            "queued": [],
            "finished": [],
            "used": 0,
            "cap": 0,
            "used_text": "",
            "cap_text": "",
            "held": 0,
        },
    }

    def open(
        self,
        ctx: dict[str, Any],
        settings: dict[str, Any] | None,
        *,
        subject: str = "",
        cell: str = "",
    ) -> _NoArt:
        return _NoArt()

    def folder(self, data_dir: Path) -> Path:
        return Path(data_dir) / self.name

    def __getattr__(self, name: str) -> Any:
        default = self._DEFAULTS.get(name)
        return lambda *a, **k: default


FAL = "https://fal.run/"
T2I_ENDPOINT = (
    "fal-ai/nano-banana"  # not used: every painting here has a photograph to start from
)
MODELS: dict[str, dict[str, str]] = {
    "nano_banana": {
        "label": "Nano Banana",
        "endpoint": "fal-ai/nano-banana/edit",
        "cost": "about US$0.04",
        "cents": 4,
    },
    "nano_banana_pro": {
        "label": "Nano Banana Pro",
        "endpoint": "fal-ai/nano-banana-pro/edit",
        "cost": "about US$0.15",
        "cents": 15,
    },
    "flux_kontext": {
        "label": "FLUX.1 Kontext Pro",
        "endpoint": "fal-ai/flux-pro/kontext",
        "cost": "about US$0.04",
        "cents": 4,
    },
    "gpt_image": {
        "label": "GPT Image 2",
        "endpoint": "openai/gpt-image-2/edit",
        "cost": "about US$0.05",
        "cents": 5,
    },
    "seedream": {
        "label": "Seedream 4",
        "endpoint": "fal-ai/bytedance/seedream/v4/edit",
        "cost": "about US$0.03",
        "cents": 3,
    },
}
DEFAULT_MODEL = "nano_banana"
GEN_TIMEOUT_S = 120
GEN_DAILY_CAP = 12  # paintings a day, the setting before the budget was in dollars
GEN_DAILY_CENTS = 400  # US$4 a day, about a hundred Nano Banana paintings
TYPICAL_CENTS = 4
UNLIMITED = 10**9
GEN_RETRY_S = 6 * 3600
PER_FETCH = 3
BATCH_PARALLEL = 3
_PARALLEL = BATCH_PARALLEL
_PER_FETCH = PER_FETCH
STOPPED = "stopped"
PROMPT_VERSION = 1
CUT_VERSION = 1  # the cut-outs are redone from the kept paintings when this changes
TRIM = 0.01  # the share of the page's shorter side cut off each edge before the cut-out
NOTE_NO_KEY = (
    "AI pictures need a fal.ai key: this widget's own, or the app's under Settings."
)
NOTE_GEN_CAP = "AI paintings: to-day's budget of {cap} is spent; more to-morrow."
NOTE_NO_PHOTO = "No photograph of this vehicle or her class to paint from."

# ----------------------------------------------------------------------
# Styles: an id, a label and how the picture is made. {kind} is "tram" or "train".
# ----------------------------------------------------------------------
STYLES: dict[str, tuple[str, str]] = {
    "poster": (
        "Vintage railway poster",
        "a vintage mid-century railway poster illustration in gouache: flat confident shapes, a limited palette of warm tones, crisp edges with a little paper texture, the {kind} rendered with affection and accuracy",
    ),
    "engraving_colour": (
        "Hand-coloured engraving",
        "a hand-coloured copperplate engraving from a nineteenth-century technical atlas: fine engraved linework and stippling under delicate watercolour washes, the livery's colours kept true",
    ),
    "engraving": (
        "Black-ink engraving",
        "a black-ink copperplate engraving from a Victorian encyclopaedia: fine cross-hatching and stippling, no colour at all",
    ),
    "technical": (
        "Technical ink drawing",
        "a precise technical ink illustration from a manufacturer's brochure: clean ruled linework, light hatching for shadow, the panel lines, doors and windows drawn, no colour",
    ),
    "watercolour": (
        "Loose watercolour",
        "a loose, airy watercolour with soft wet edges, a few precise details on the cab and the pantograph and plenty of white space",
    ),
    "woodblock": (
        "Japanese woodblock print",
        "a Japanese woodblock print: flat areas of colour with a fine dark keyline and the texture of washi paper",
    ),
    "linocut": (
        "Linocut",
        "a bold linocut print in black ink with one muted colour: strong carved lines, simple shapes, a little texture from the block",
    ),
    "deco": (
        "Art Deco travel poster",
        "an Art Deco travel poster of the 1930s: streamlined geometric shapes, sweeping speed lines, a palette of cream, teal and vermilion, airbrushed gradients",
    ),
    "midcentury": (
        "Mid-century flat illustration",
        "a mid-century modern illustration in the manner of a 1960s railway brochure: the {kind} reduced to clean geometric shapes, flat colour in a handful of tones, no outlines",
    ),
    "pencil": (
        "Pencil study",
        "a careful graphite pencil study: soft shading, the steel's sheen suggested with a few highlights, no colour",
    ),
    "pointillism": (
        "Pointillism",
        "a pointillist painting built from thousands of small dots of pure colour, the {kind}'s form emerging from the dots",
    ),
    "blueprint": (
        "Blueprint",
        "a cyanotype blueprint: fine white linework on deep Prussian blue paper, dimension ticks and construction lines",
    ),
    "cutaway": (
        "Magazine cutaway",
        "a 1950s magazine cutaway illustration: the body opened to show the driver's cab, the seats and the standing passengers in neat sections, tiny figures holding the straps, painted in gouache with a draughtsman's precision",
    ),
    "stamp": (
        "Postage stamp",
        "a finely engraved postage stamp: the {kind} in the stamp's engraved linework within an ornate perforated border, the country and value lettered in the stamp's own style, the whole stamp filling the picture",
    ),
    "tattoo": (
        "Tattoo flash",
        "an old-school tattoo flash sheet drawing: bold black outlines, a flat limited palette of red, yellow and green, simple shading, a ribbon banner curling beneath",
    ),
    "riso": (
        "Risograph",
        "a two-colour risograph print in fluorescent pink and blue ink only: the {kind} reduced to two overprinted flat ink layers with grainy texture, slight misregistration and coarse halftone shading, no photographic detail at all",
    ),
    "pixel": (
        "Pixel art",
        "coarse pixel art as from a 16-bit video game: the {kind} drawn at a tiny resolution of about 96 pixels across and scaled up so every square pixel is plainly visible, a palette of sixteen colours, stair-stepped edges, dithered shading, nothing smooth",
    ),
    "lowpoly": (
        "Low-poly render",
        "a low-polygon 3D render: the {kind} built from a few hundred large flat-shaded triangles so that every facet shows as a visible flat plane with a hard edge, no smooth surfaces, soft studio lighting",
    ),
    "papercraft": (
        "Paper craft",
        "a paper model of the {kind} made from cut and folded card, photographed on a table: visible folds, glued tabs and the cut edges of the card, lit from the side",
    ),
    "toybrick": (
        "Toy bricks",
        "a model of the {kind} built from interlocking plastic toy bricks, rendered as a product photograph: rows of round studs along the top, every curve stepped into blocky brick shapes, glossy moulded plastic, the livery in brick colours",
    ),
    "charcoal": (
        "Charcoal",
        "a drawing in charcoal sticks on rough toothy paper: visibly smudged by hand, strokes and finger-rubbed shading plain to see, a few sharp lines on the cab and the pantograph, nothing photographic, no colour",
    ),
    "stained_glass": (
        "Stained glass",
        "a stained-glass window design: the {kind} made of flat panes of coloured glass separated by thick black lead lines, the light coming through the glass",
    ),
    "gilt": (
        "Illuminated manuscript",
        "an illuminated manuscript miniature: the {kind} painted in tempera with gold leaf on the cab and the pantograph, a fine red and blue filigree border",
    ),
    "pulp": (
        "Pulp cover",
        "a 1950s pulp science-fiction magazine cover painting: airbrushed, dramatic, saturated, the {kind} heroic against a lurid city",
    ),
    "synthwave": (
        "Synthwave",
        "a synthwave illustration: neon pink and cyan on a dark purple ground, a glowing grid horizon and a striped sun, the {kind} in chrome",
    ),
    "chalk": (
        "Chalkboard",
        "a chalk drawing on a dark blackboard: white and coloured chalk, soft smudges, the lines a little dusty",
    ),
}
STYLE_IDS = (*STYLES, "builder", "custom")
# Styles without colour: the prompt asks for black, white and grey only
MONO = {"engraving", "technical", "pencil", "charcoal"}
# Styles that make a whole picture on their own ground, never cut out
OWN_PAPER = {"blueprint", "stamp", "synthwave", "chalk", "gilt", "pulp"}
CUSTOM_EXAMPLE = "a chalk pastel drawing on grey paper, soft and smudgy, with the sun catching the side of the {kind}"
CUSTOM_PRESETS = [
    {
        "label": "Gouache poster",
        "value": "a vintage railway poster in gouache: flat confident shapes, a warm limited palette, crisp edges",
    },
    {
        "label": "Cutaway",
        "value": "a 1950s magazine cutaway illustration: the body opened to show the seats and the driver's cab, labelled in small neat capitals",
    },
    {"label": "Chalk pastel", "value": CUSTOM_EXAMPLE},
    {
        "label": "Railway art",
        "value": "a traditional oil painting in the manner of classic railway art: dramatic light, polished steel, confident brushwork",
    },
    {
        "label": "Comic ink",
        "value": "a comic-book ink drawing with bold outlines, hatched shadows and flat halftone colour",
    },
    {
        "label": "Neon line",
        "value": "a single-weight neon line drawing on white: one continuous glowing line describing the {kind}",
    },
]
_CUSTOM_RE = re.compile(r"custom[a-z]{8}")

BUILDER: dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]] = {
    "medium": (
        "Medium",
        "gouache",
        (
            ("gouache", "Gouache", "a flat gouache painting"),
            ("watercolour", "Watercolour", "a loose watercolour painting"),
            ("oil", "Oil paint", "an oil painting with visible brushwork"),
            ("pastel", "Soft pastel", "a soft pastel drawing"),
            ("coloured_pencil", "Coloured pencil", "a coloured-pencil drawing"),
            ("pencil", "Graphite pencil", "a graphite pencil drawing"),
            ("ink", "Pen and ink", "a pen-and-ink drawing"),
            ("engraving", "Engraving", "a copperplate engraving"),
            ("lithograph", "Lithograph", "a hand-coloured lithograph"),
            ("woodblock", "Woodblock", "a woodblock print"),
            ("linocut", "Linocut", "a linocut print"),
            ("screenprint", "Screen print", "a screen print with a few flat inks"),
            ("vector", "Flat vector", "a flat vector illustration"),
            ("airbrush", "Airbrush", "an airbrushed illustration"),
            ("papercut", "Paper cut", "layered cut paper"),
        ),
    ),
    "period": (
        "Period or school",
        "",
        (
            ("", "None in particular", ""),
            ("1930s", "1930s travel poster", "in the manner of a 1930s travel poster"),
            (
                "1950s",
                "1950s railway brochure",
                "in the manner of a 1950s railway brochure",
            ),
            ("1960s", "1960s streamline", "in the streamlined style of the 1960s"),
            (
                "victorian",
                "Victorian technical plate",
                "in the manner of a Victorian technical plate",
            ),
            ("bauhaus", "Bauhaus", "in the Bauhaus manner"),
            ("ukiyoe", "Ukiyo-e", "in the manner of a Japanese ukiyo-e print"),
            ("deco", "Art Deco", "in the Art Deco manner"),
            ("retrofuture", "Retro-futurism", "in a retro-futurist manner"),
        ),
    ),
    "palette": (
        "Palette",
        "",
        (
            ("", "As the livery is", ""),
            ("limited", "A few warm tones", "in a limited palette of a few warm tones"),
            ("muted", "Muted", "in muted, dusty colours"),
            ("vivid", "Vivid", "in vivid saturated colour"),
            ("duotone", "Two inks", "in two inks only"),
            ("mono", "Black and white", "in black and white only"),
            ("sepia", "Sepia", "in sepia tones"),
        ),
    ),
    "line": (
        "Line",
        "",
        (
            ("", "As the medium has it", ""),
            ("fine", "Fine", "with fine delicate line"),
            ("bold", "Bold", "with bold confident outlines"),
            ("none", "No outlines", "with no outlines, shapes meeting edge to edge"),
            ("sketchy", "Sketchy", "with loose sketchy line"),
        ),
    ),
    "texture": (
        "Texture",
        "",
        (
            ("", "As the medium has it", ""),
            ("paper", "Paper grain", "with visible paper grain"),
            (
                "print",
                "Print texture",
                "with the texture of a print, slightly misregistered",
            ),
            ("smooth", "Smooth", "smooth and clean"),
            ("halftone", "Halftone", "with halftone dots in the shading"),
        ),
    ),
    "mood": (
        "Mood",
        "",
        (
            ("", "Plain", ""),
            ("heroic", "Heroic", "heroic and dramatic, lit from below"),
            ("calm", "Calm", "calm and serene"),
            ("nostalgic", "Nostalgic", "warm and nostalgic"),
            ("technical", "Precise", "precise and matter-of-fact"),
        ),
    ),
}


def build_how(options: dict[str, Any]) -> str:
    """The style builder's picks as one style description."""
    parts: list[str] = []
    for name, (_label, default, choices) in BUILDER.items():
        pick = str(options.get(f"build_{name}") or default)
        phrase = next((p for v, _l, p in choices if v == pick), "")
        if name == "medium":
            phrase = (phrase or choices[0][2]) + " of the {kind}"
        if phrase:
            parts.append(phrase)
    extra = " ".join(str(options.get("build_extra") or "").split())[:300]
    if extra:
        parts.append(extra)
    return ", ".join(parts)


# ----------------------------------------------------------------------
# Backdrops: none keeps the vehicle alone on white paper for the cut-out;
# the rest make a whole picture that fills its frame.
# ----------------------------------------------------------------------
BACKDROPS: dict[str, tuple[str, str]] = {
    "none": ("None: the vehicle alone, cut out on the page", ""),
    "street": (
        "A city street",
        "a city street in the same style, shopfronts and the overhead wires above, the light of the time of day",
    ),
    "platform": (
        "At the platform",
        "a railway platform in the same style, the canopy and the station's signs beyond",
    ),
    "bridge": (
        "Over a bridge",
        "a river bridge in the same style, the water below and the city behind",
    ),
    "country": (
        "Through the country",
        "open country in the same style, paddocks, gum trees and a wide sky",
    ),
    "night": (
        "Night",
        "the city at night in the same style, the vehicle's lights on and its windows glowing",
    ),
    "rain": (
        "Rain",
        "a wet evening in the same style, the rails and the road shining with reflections",
    ),
    "ornate": (
        "Ornate framed plate",
        "a decorative backdrop in the same style, a patterned or foliate ground within a drawn ornamental border that fills the whole picture to its edges, the page's own art in that style and nothing modern",
    ),
    "live": ("The street as it is now: time of day and weather", ""),
}

GEN = (
    "Draw the {typ} in the reference photo{num} in {op} livery, afresh as {style}: a new illustration of this "
    "{kind} in that style, not the photo restyled. The reference photo is for identity only: its exact livery, "
    "colours, the shape of the cab, the doors and windows, and the proportions are kept. The {kind} {view}{scene}. "
    "The operator's markings and the number on the cab are reproduced as in the photograph. {page} No text or "
    "lettering beyond the livery's own and the number, no border, no signature.{outline}"
)
GEN_FAITHFUL = (
    "Render the {typ} in the reference photo{num} in {op} livery, as {style}: a faithful translation of this very "
    "photograph into that medium. The photograph is the authority: the same {kind} at the same angle as "
    "photographed; every element of the livery as it actually appears in the photograph, its colours, markings, "
    "any advertising wrap and the number, and the particular details of the vehicle, the cab, the doors, the "
    "windows, the pantograph and the bogies, reproduced exactly as they are in the photograph; only the medium "
    "changes{scene}. Everything else in the photograph, the street, the platform, other vehicles, people, poles and "
    "wires, is left out unless the backdrop asks for it. {page} No text or lettering beyond the livery's own, no "
    "border, no signature.{outline}"
)
GEN_SIDE = (
    "Draw the {typ} in the first reference photo{num} in {op} livery, as {style}, seen exactly from the side at "
    "eye level: a pure side elevation, as in a builder's drawing, the whole {kind} level and centred, every door, "
    "window and section along her length shown. The first photograph is the authority for her livery, colours, "
    "markings and the shape of her cab; the second picture shows a {kind} of the same class from the side, and is "
    "the authority for her length, the number of sections, the number and spacing of the doors and windows, the "
    "roof equipment and the bogies; the number, route sign and any advertising in the second picture are another "
    "vehicle's and are not drawn. Everything else in either picture, the street, the platform, other vehicles, "
    "people, poles and wires, is left out{scene}. {page} No text or lettering beyond the livery's own, no border, "
    "no signature.{outline}"
)
SIDE_REF = (
    " The second picture shows a {kind} of the same class from the side: take her length, her sections and the "
    "spacing of her doors and windows from it, never its number, route sign or advertising, and keep everything "
    "else from the first photograph."
)
FIDELITIES = ("photo", "free", "side")
PAGE_ALONE = (
    "The {kind} alone on plain, pure white paper with a clear margin all round and nothing else on the page: "
    "no street, track, platform, wires, poles, ground, shadow or ornament round or behind it beyond a short "
    "length of rail under its wheels. The style says how the {kind} is drawn and nothing else."
)
PAGE_WHOLE = "The picture complete, filling the frame edge to edge."
OUTLINE = (
    " The whole {kind} is drawn with a fine, clear, unbroken dark contour line along its entire outline, body, "
    "cab, roof equipment and bogies, so that a white or pale part is never lost against the white paper."
)
VIEWS = {
    "side": "seen from the side and a little to the front, at eye level",
    "profile": "seen exactly from the side at eye level, a pure profile as in a builder's drawing, the whole vehicle level and centred",
    "quarter": "seen from the front three-quarter, coming towards the viewer",
    "head": "seen head-on, the cab and the destination sign filling the middle",
}
MONO_WORDS = (
    "its exact livery, colours, the shape",
    "its exact livery, rendered without any colour (black, white and grey only), the shape",
)


def style_of(style: str | None) -> tuple[str, str]:
    if style in STYLES:
        return STYLES[style]
    if style and _CUSTOM_RE.fullmatch(style):
        return ("Your own style", _CUSTOM.get(style, ""))
    return STYLES["poster"]


def custom_tag(how: str) -> str:
    digest = hashlib.sha1(" ".join(how.split()).lower().encode()).hexdigest()
    return "custom" + "".join(
        "abcdefghijklmnopqrstuvwxyz"[int(c, 16) % 26] for c in digest[:8]
    )


_CUSTOM: dict[str, str] = {}


def register_custom(how: str) -> str:
    tag = custom_tag(how)
    _CUSTOM[tag] = how
    return tag


def key_for(
    who: str,
    style: str,
    backdrop: str = "none",
    view: str = "side",
    model: str = DEFAULT_MODEL,
    weather: str = "",
    fidelity: str = "photo",
    photo: str = "",
    side: str = "",
) -> str:
    """The painting's key: the vehicle (or, painted from a class photograph,
    the class and that photograph), the style and what else shapes it."""
    parts = [N.slug(who), style, f"p{PROMPT_VERSION}"]
    if photo:
        parts.append("ph_" + hashlib.sha1(photo.encode()).hexdigest()[:8])
    if side:
        parts.append("sd_" + hashlib.sha1(side.encode()).hexdigest()[:8])
    if fidelity in ("free", "side"):
        parts.append(fidelity)
    if fidelity != "free":
        view = "side"  # as photographed, or side-on: the view is settled
    if backdrop == "live":
        parts.append(f"bd_{N.slug(weather or 'midday_clear')[:24]}")
    elif backdrop != "none":
        parts.append(f"bd_{backdrop}")
    if view != "side":
        parts.append(f"v_{view}")
    if model != DEFAULT_MODEL:
        parts.append(f"m_{model}")
    return "--".join(parts)


def prompt_for(job: dict[str, Any]) -> str:
    kind = "tram" if job.get("cls") == "tram" else "train"
    _label, how = style_of(job.get("style"))
    how = str(job.get("how") or how).replace("{kind}", kind)
    backdrop = str(job.get("backdrop") or "none")
    scene = (
        f", across {BACKDROPS[backdrop][1]}"
        if backdrop in BACKDROPS and backdrop not in ("none", "live")
        else ""
    )
    if backdrop == "live":
        scene = (
            f", across {job.get('weather_words') or 'a city street in the same style'}"
        )
    if backdrop == "ornate":
        scene = (
            ", as the single subject of an ornate framed plate: the "
            + kind
            + " large in the middle on "
            + BACKDROPS["ornate"][1]
        )
    alone = backdrop == "none" and str(job.get("style") or "") not in OWN_PAPER
    num = f", number {job['num']}," if job.get("num") and job.get("exact") else ""
    fill = {
        "typ": job.get("typ") or kind,
        "num": num,
        "op": job.get("op") or "its own",
        "style": how,
        "kind": kind,
        "page": (PAGE_ALONE if alone else PAGE_WHOLE).replace("{kind}", kind),
        "outline": OUTLINE.replace("{kind}", kind) if alone else "",
    }
    fidelity = str(job.get("fidelity") or "photo")
    set_scene = (
        scene.replace(", across ", ", set across ")
        if scene and backdrop != "ornate"
        else scene
    )
    if fidelity == "side" and job.get("side"):
        text = GEN_SIDE.format(**fill, scene=set_scene)
    elif fidelity == "photo":
        text = GEN_FAITHFUL.format(**fill, scene=set_scene)
        if job.get("side"):
            text += SIDE_REF.format(kind=kind)
    else:
        # drawn afresh, or side-on without a side view: the photograph alone, in the view asked for
        view = "profile" if fidelity == "side" else str(job.get("view") or "side")
        text = GEN.format(**fill, view=VIEWS.get(view, VIEWS["side"]), scene=scene)
        if job.get("side"):
            text += SIDE_REF.format(kind=kind)
    if not job.get("exact"):
        text += f" The photograph is of another {kind} of the same class: keep the class's shape and the operator's standard livery, and leave out any advertising wrap or fleet number particular to that one."
    if str(job.get("style") or "") in MONO:
        text = text.replace(*MONO_WORDS)
    return text


def model_of(model: str | None) -> str:
    return model if model in MODELS else DEFAULT_MODEL


# ----------------------------------------------------------------------
# Files, budget, state
# ----------------------------------------------------------------------
ART: Any = (
    artwork.Collection(
        "trackside_core",
        "paintings",
        make=lambda job: make(job),
        label="Trackside",
        title="Painting trains and trams",
        facet="style",
        facet_label=lambda s: style_of(s)[0],
    )
    if artwork is not None
    else _NoArtwork()
)
UNLIMITED = artwork.UNLIMITED if artwork is not None else 10**9


def gen_dir(data_dir: Path) -> Path:
    """The paintings' folder, the collection's, for the tools and the tests."""
    return ART.folder(Path(data_dir))


def _data_dir(path: Path) -> Path:
    """The plugin's data dir from either it or the paintings folder under it."""
    path = Path(path)
    return path.parent if path.name == ART.name else path


def budget_used(path: Path) -> int:
    return ART.budget_used(_data_dir(path))


def daily_cap(settings: dict[str, Any] | None) -> int:
    return ART.cap(settings)


def dollars(cents: int) -> str:
    return "no limit" if cents >= UNLIMITED else f"US${cents / 100:.2f}"


def cost_cents(model: str) -> int:
    return int(MODELS.get(model, MODELS[DEFAULT_MODEL]).get("cents") or TYPICAL_CENTS)


def whole(value: Any, default: int, low: int = 0, high: int = 100000) -> int:
    with contextlib.suppress(TypeError, ValueError):
        if str(value).strip() != "":
            return min(high, max(low, int(float(value))))
    return default


def state(key: str, path: Path) -> str:
    return ART.state(key, _data_dir(path))


def why_failed(key: str, path: Path) -> str:
    return ART.why_failed(key, _data_dir(path))


def archive(path: Path, key: str) -> None:
    ART.archive(key, _data_dir(path))


# ----------------------------------------------------------------------
# The cut-out: a painting on white paper becomes a PNG with alpha, the
# paper's colour read from the corners and the noise of its texture allowed
# for, so a cream or grainy paper cuts as cleanly as a white one.
# ----------------------------------------------------------------------
def cut_out(raw: bytes) -> bytes | None:
    """The painting as a PNG with alpha. The paper is what is paper-coloured
    and reaches the edge of the page; everything else, a white body inside
    its contour line included, is the vehicle and stays."""
    try:
        import numpy as np
        from PIL import Image, ImageFilter
    except ImportError:  # pragma: no cover
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    # the outermost rim goes first: a darker edge, a vignette or a frame line the model
    # left along the page's edge would otherwise stay as a line
    trim = max(6, int(round(min(im.size) * TRIM)))
    if im.width > 4 * trim and im.height > 4 * trim:
        im = im.crop((trim, trim, im.width - trim, im.height - trim))
    a = np.asarray(im).astype(float)
    H, W = a.shape[:2]
    c = 30
    corners = np.concatenate(
        [
            a[:c, :c].reshape(-1, 3),
            a[:c, -c:].reshape(-1, 3),
            a[-c:, :c].reshape(-1, 3),
            a[-c:, -c:].reshape(-1, 3),
        ]
    )
    paper = np.median(corners, axis=0)
    # the paper's own grain, measured on the corner pixels that are paper (a frame line or a
    # wingtip in a corner would otherwise read as grain and let the whole page through)
    spread = np.sqrt(((corners - paper) ** 2).sum(-1))
    near = spread[spread < 60]
    noise = float(near.std()) if len(near) > 50 else 0.0
    d = np.sqrt(((a - paper) ** 2).sum(-1))
    thr = max(22.0, 10 + 4 * noise)
    paperlike = d < thr
    # the paper: the paper-coloured region joined to the page's edge, found on a half-size mask.
    # A frame line or a vignette the model drew round the page would wall the flood in and keep
    # the whole page; then the seeds move inward, a little at a time, until the page lets go.
    s = 2
    small = (
        paperlike[: H // s * s, : W // s * s]
        .reshape(H // s, s, W // s, s)
        .all(axis=(1, 3))
    )
    h, w = small.shape
    keep_small = None
    for inset in (0, 0.03, 0.06, 0.1, 0.15):
        iy, ix = int(h * inset), int(w * inset)
        bg = np.zeros_like(small)
        stack = []
        for x in range(ix, w - ix):
            for y in (iy, h - 1 - iy):
                if small[y, x]:
                    stack.append((y, x))
        for y in range(iy, h - iy):
            for x in (ix, w - 1 - ix):
                if small[y, x]:
                    stack.append((y, x))
        for y, x in stack:
            bg[y, x] = True
        while stack:
            y, x = stack.pop()
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and small[ny, nx] and not bg[ny, nx]:
                    bg[ny, nx] = True
                    stack.append((ny, nx))
        # everything outside the seed ring is paper too, frame and all; and the page's outermost
        # rim always, since a darker edge the model left there would otherwise stay as a line
        rim = max(1, int(round(min(h, w) * 0.012)))
        for lo in (iy, rim):
            bg[:lo, :] = True
            bg[h - lo :, :] = True
        for lo in (ix, rim):
            bg[:, :lo] = True
            bg[:, w - lo :] = True
        keep_small = ~bg
        if keep_small.mean() < 0.6:
            break
    # stray slivers (a stroke left at an edge, a speck) go; the vehicle and anything of size stays
    lab = np.zeros(keep_small.shape, dtype=np.int32)
    sizes: dict[int, int] = {}
    boxes: dict[int, tuple[int, int, int, int]] = {}
    n = 0
    for y0 in range(h):
        for x0 in range(w):
            if keep_small[y0, x0] and not lab[y0, x0]:
                n += 1
                stack = [(y0, x0)]
                lab[y0, x0] = n
                count = 0
                y_lo = y_hi = y0
                x_lo = x_hi = x0
                while stack:
                    y, x = stack.pop()
                    count += 1
                    y_lo, y_hi = min(y_lo, y), max(y_hi, y)
                    x_lo, x_hi = min(x_lo, x), max(x_hi, x)
                    for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                        if (
                            0 <= ny < h
                            and 0 <= nx < w
                            and keep_small[ny, nx]
                            and not lab[ny, nx]
                        ):
                            lab[ny, nx] = n
                            stack.append((ny, nx))
                sizes[n] = count
                boxes[n] = (y_lo, y_hi, x_lo, x_hi)
    if sizes:
        biggest = max(sizes.values())
        floor = max(40, int(0.004 * h * w), biggest // 40)
        for k, count in sizes.items():
            y_lo, y_hi, x_lo, x_hi = boxes[k]
            bh, bw = y_hi - y_lo + 1, x_hi - x_lo + 1
            # a frame line the model drew round the page, or what is left of one: a long thin
            # run on its own, or a hollow rectangle; never part of the vehicle, which is one piece
            line = (bh <= 0.03 * h and bw >= 0.3 * w) or (
                bw <= 0.03 * w and bh >= 0.3 * h
            )
            hollow = bw >= 0.5 * w and bh >= 0.5 * h and count < 0.06 * bh * bw
            if count < floor or line or hollow:
                keep_small[lab == k] = False
    keep = np.zeros((H, W), dtype=bool)
    keep[: h * s, : w * s] = np.kron(keep_small, np.ones((s, s), dtype=bool))
    # the edge: soft where the paint meets the paper, hard inside the outline
    soft = np.clip((d - thr) / 35, 0, 1)
    hard = (
        Image.fromarray((keep * 255).astype("uint8"))
        .filter(ImageFilter.MinFilter(3))
        .filter(ImageFilter.MaxFilter(3))
    )
    hard_a = np.asarray(hard) > 0
    alpha = np.where(hard_a, np.maximum(soft, 0.98), 0.0)
    alpha8 = np.asarray(
        Image.fromarray((alpha * 255).astype("uint8")).filter(
            ImageFilter.GaussianBlur(0.6)
        )
    )
    share = (alpha8 > 40).mean()
    if share < 0.004 or share > 0.9:
        return None
    rgba = np.dstack([a.astype("uint8"), alpha8])
    out = Image.fromarray(rgba.astype("uint8"), "RGBA")
    bbox = Image.fromarray(alpha8).point(lambda p: 255 if p > 40 else 0).getbbox()
    if bbox:
        out = out.crop(bbox)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def picture(
    art: Any, key: str, px: int, levels: str, whole_: bool
) -> dict[str, Any] | None:
    """A kept painting for the page: the whole picture as a JPEG data URI, or
    the cut-out as a PNG with alpha, resized for the cell and the panel and
    cached by the collection."""
    c, data_dir = art.c, art.data_dir
    if c is None:
        return None
    path = c.path(key, data_dir)
    if path is None:
        return None
    meta = c.meta(key, data_dir)
    credit = str(meta.get("credit") or "")
    if whole_:
        src = art.url(key, px=px, levels=levels)
        return (
            {
                "src": src,
                "kind": "painting",
                "whole": True,
                "credit": credit,
                "aspect": N.aspect_of(src),
            }
            if src
            else None
        )
    variant = f"cut{CUT_VERSION}"
    cut = c.folder(data_dir) / f"{key}__{variant}.png"
    if not cut.exists():
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        png = cut_out(raw)
        if not png:
            c.note_error(
                key,
                data_dir,
                "the painting could not be cut out",
                title=str(meta.get("title") or key),
            )
            return None
        N.write(cut, png)
    src = art.url(key, px=px, levels=levels, cut=True, variant=variant)
    return (
        {
            "src": src,
            "kind": "painting",
            "whole": False,
            "credit": credit,
            "aspect": N.aspect_of(src),
        }
        if src
        else None
    )


def _local(path: str) -> bytes | None:
    with contextlib.suppress(OSError):
        return Path(path).expanduser().read_bytes()
    return None


def make(job: Any) -> tuple[bytes, dict[str, Any]]:
    """One painting, on a worker thread: the prompt from the payload, the
    photograph fetched with the widget's own User-Agent and handed over
    inline (fal.ai's own fetch of a Commons picture is refused), the picture
    from fal.ai through the collection's budget. Raises ArtworkError with why not."""
    j = job.payload
    photo = str(j.get("photo") or "")
    if not photo:
        raise artwork.ArtworkError("no photo")
    model = model_of(j.get("model"))
    prompt = prompt_for(j)
    if not photo.startswith("data:"):
        raw_ref = N.cached_get(job.data_dir, photo, ua=str(j.get("ua") or ""))
        if not raw_ref:
            raise artwork.ArtworkError("the photograph could not be read")
        photo = _fal.data_uri(raw_ref)
    refs = [photo]
    side = str(j.get("side") or "")
    if side and model != "flux_kontext":  # Kontext takes one picture
        raw_side = N.cached_get(job.data_dir, side, ua=str(j.get("ua") or ""))
        if raw_side:
            refs.append(_fal.data_uri(raw_side))
    raw = job.fal.generate(
        model, prompt, refs=refs, aspect=str(j.get("aspect") or "4:3"), retries=1
    )
    label = MODELS[model]["label"]
    whose = "her own photograph" if j.get("exact") else "a photograph of her class"
    credit = f"painted by {label} from {whose} by {j.get('by') or 'a Commons contributor'} (Wikimedia Commons, {j.get('lic') or 'open licence'})"
    if len(refs) > 1:
        credit += (
            f", and a side view by {j.get('side_by') or 'a Commons contributor'}"
            + (f" ({j['side_lic']})" if j.get("side_lic") else "")
        )
    meta = {
        "num": j.get("num"),
        "typ": j.get("typ"),
        "op": j.get("op"),
        "style": j.get("style"),
        "how": j.get("how") or "",
        "backdrop": j.get("backdrop") or "none",
        "model": model,
        "credit": credit,
        "photo_by": j.get("by") or "",
        "photo_link": j.get("link") or "",
        "by": j.get("by") or "",
        "link": j.get("link") or "",
        "exact": bool(j.get("exact")),
        "side": side if len(refs) > 1 else "",
        "side_by": j.get("side_by") or "",
        "prompt": prompt,
        "prompt_version": PROMPT_VERSION,
    }
    return raw, meta
