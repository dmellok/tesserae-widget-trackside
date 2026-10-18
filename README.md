# Trackside

Four Tesserae widgets for the trains and trams near you right now, installed together as one bundle:

| widget | the page |
|---|---|
| **Trackside: Photo** | the nearest train or tram as a full-bleed photograph of the very vehicle, the next few along a strip |
| **Trackside: Plate** | one vehicle painted from her photograph, in a ruled frame, her particulars beside her; or one of four modern looks in her operator's colours |
| **Trackside: Line-up** | everything on the rails near you on one page, each painted and cut out, sized by her length; or a log of photograph cards |
| **Trackside: Scene** | one whole painting across the page: the vehicle in a street, at the platform, over a bridge, through the country, at night or in the rain |

**Melbourne through the PTV Timetable API**: with a PTV developer id and key (emailed by PTV on request), every Metro train within reach shows with her live position, her cars, PTV's own description of the set ("6 Car Siemens") and where she is going, and each tram by fleet number at the stop near you she is due at, with the minutes to go (the API gives no tram positions, and nothing live for V/Line). The key never leaves your server: each request is signed with it.

**Positions** come from GTFS-Realtime vehicle-position feeds, the standard most transit agencies publish. **Melbourne** reads Transport Victoria's three feeds (Metro Trains, Yarra Trams and V/Line) with a free key: sign up at opendata.transport.vic.gov.au, make an API key, and paste it into the Trackside Core settings. Boston (MBTA), Brisbane (Queensland Rail) and the Gold Coast (G:link) need no key. **Anywhere else**, give the agency's VehiclePositions link, the header its key goes in and the key; the widget reads the feed itself, with no protobuf library. The feeds are asked at most every thirty seconds, which keeps inside Transport Victoria's 24 calls a minute.

**Melbourne's fleet is known by number**: a tram's class from its fleet number (Z3, A, B2, C, C2, D1, D2, E, E2, G and the heritage W8), a train's from its car numbers (Comeng, Siemens Nexas, X'Trapolis 100, the High Capacity Metro Train), with the tram's route and the train's line read from the feed's route ids. Elsewhere the page shows the operator's own label and route.

**Photographs** come from Wikimedia Commons, which keeps a category for every Melbourne tram and train class with the fleet number in most file titles: the widget finds a photograph of the very vehicle where there is one, and otherwise a photograph of another of her class, marked on the page as such. Pictures under a no-derivatives licence are never used. For another agency, give the operator's name and the widget looks for each vehicle by name and number.

**Pictures** are the photographs, or AI paintings: each vehicle painted once from her photograph through fal.ai, in a style, on white paper so she cuts out cleanly, at about US$0.04 a painting. A vehicle painted from a photograph of her class is painted in her class's standard livery, without another tram's advertising wrap, and is shared by every vehicle of that class. **Paint** chooses every vehicle with a photograph, or only those with one of their own. **Side views**: most photographs of a tram or a train are taken from the front three-quarter, so the widget carries one side-on picture of each Melbourne class, chosen by hand from Commons (Z, A, B2, C, C2, D1, D2, E, G, W, Comeng, Nexas, X'Trapolis, HCMT, VLocity), and hands it to the painter as a second picture for her length, her sections and the spacing of her doors. **Faithfulness** set to Side-on (the Line-up's default) paints a pure side elevation from both pictures, so a line-up reads like a fleet roster; your own side views, for any class or agency, go in the Trackside Core settings (one a line, class = link). Twenty-six styles, **Build my own** and **Your own prompt**. The Plate and the Scene take a **backdrop**: a city street, at the platform, over a bridge, through the country, night, rain, an ornate framed plate, or the street as it is now (time of day and weather from Open-Meteo).

**When** is a setting on every page: what is within reach at the moment the page is drawn, or everything that passed since the page was last drawn for a panel, each vehicle at her closest approach with the time. The widget keeps a watch on the place between renders, asking the feeds every minute, and stops three hours after a page last asked.

**Settings** live on Trackside Core: the Transport Victoria key, the fal.ai key (or the one under Settings › Generative AI), a contact for Wikimedia Commons, the daily painting budget, and how many paintings start per preview and run at once. The paintings page is under Plugins › Trackside Core.

## Install

Needs Tesserae 0.463 or later: the paintings are kept, queued, budgeted and listed by the server's own Artwork support (`app.artwork`).

From the Tesserae catalog as the Trackside bundle, or by hand: copy the five folders (`trackside_core`, `trackside_photo`, `trackside_plate`, `trackside_lineup`, `trackside_scene`) into your plugins folder together; the four widgets load their shared code from `trackside_core` by path.

## Development

The tests and the tools import Tesserae's `app.artwork`; a checkout of the server beside this repo is found on its own, or point `TESSERAE_REPO` at one:

```sh
PYTHONPATH=../tesserae python -m pytest trackside_core/tests -q        # offline: every call on the network is faked
python trackside_core/tools/manifests.py                                # rewrites the five plugin.json from one description
python trackside_core/tools/preview.py --widget lineup pictures=photos  # a live render (the Gold Coast, no key) to trackside_core/tools/out/
VIC_KEY=... python trackside_core/tools/preview.py --widget plate source=melbourne latitude=-37.8183 longitude=144.9671
```
