"""Stáhne lidar DMR5G + DMP1G (ČÚZK) a OSM data pro BBOX."""
import json
import re
import time
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

from config import BBOX, RAW, SHARED

FEEDS = {
    "DMR5G": "https://atom.cuzk.cz/DMR5G-SJTSK/DMR5G-SJTSK.xml",
    "DMP1G": "https://atom.cuzk.cz/DMP1G-SJTSK/DMP1G-SJTSK.xml",
}
ZIP_URL = "https://openzu.cuzk.cz/opendata/{ds}/epsg-5514/{sheet}.zip"
UA = {"User-Agent": "chomutov-3d-map/0.1"}


def sheets_in_bbox(feed_xml: str) -> list[str]:
    out = []
    for e in re.findall(r"<entry>.*?</entry>", feed_xml, re.S):
        m = re.search(r"<georss:polygon>(.*?)</georss:polygon>", e)
        code = re.search(r"_([A-Z]{4}\d{2})</inspire_dls:spatial_dataset_identifier_code>", e)
        if not m or not code:
            continue
        p = list(map(float, m.group(1).split()))
        lats, lons = p[0::2], p[1::2]
        if (max(lats) > BBOX["south"] and min(lats) < BBOX["north"]
                and max(lons) > BBOX["west"] and min(lons) < BBOX["east"]):
            out.append(code.group(1))
    return sorted(set(out))


def fetch_feed(ds: str) -> str:
    cache = SHARED / f"{ds}.xml"
    if not cache.exists():
        cache.write_bytes(requests.get(FEEDS[ds], headers=UA, timeout=120).content)
    return cache.read_text()


def fetch_sheet(ds: str, sheet: str):
    d = RAW / ds
    d.mkdir(exist_ok=True)
    if list(d.glob(f"{sheet}*.laz")):
        return
    z = d / f"{sheet}.zip"
    r = requests.get(ZIP_URL.format(ds=ds, sheet=sheet), headers=UA, timeout=600)
    r.raise_for_status()
    z.write_bytes(r.content)
    with zipfile.ZipFile(z) as zf:
        for n in zf.namelist():
            if n.lower().endswith(".laz"):
                (d / f"{sheet}.laz").write_bytes(zf.read(n))
    z.unlink()
    print(f"  {ds} {sheet} ok", flush=True)


OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def overpass(q, start=0):
    for url in (OVERPASS[start:] + OVERPASS[:start]) * 3:
        try:
            r = requests.post(url, data={"data": q}, headers=UA, timeout=400)
            r.raise_for_status()
            if b'"elements"' not in r.content[:2000]:
                raise ValueError(r.content[:300])
            return r.json()["elements"]
        except Exception as e:  # noqa: BLE001
            print(f"  {url}: {str(e)[:120]}", flush=True)
            time.sleep(5)
    raise SystemExit("OSM: všechny Overpass servery selhaly")


def fetch_osm(n=4):
    out = RAW / "osm.json"
    if out.exists():
        return
    tiles = RAW / "osm_tiles"
    tiles.mkdir(exist_ok=True)
    S, W, N, E = BBOX["south"], BBOX["west"], BBOX["north"], BBOX["east"]
    def job(k):
        a, c = divmod(k, n)
        f = tiles / f"{a}_{c}.json"
        if f.exists():
            return
        b = (f"{S + (N - S) * a / n},{W + (E - W) * c / n},"
             f"{S + (N - S) * (a + 1) / n},{W + (E - W) * (c + 1) / n}")
        print(f"OSM dlaždice {a}_{c}", flush=True)
        f.write_text(json.dumps(overpass(OSM_QUERY.replace("{b}", b), k % len(OVERPASS))))
        print(f"OSM dlaždice {a}_{c} ok", flush=True)

    # každé vlákno začíná na jiném mirroru → souběžné dotazy
    with ThreadPoolExecutor(len(OVERPASS) * 2) as ex:
        list(ex.map(job, range(n * n)))
    seen = {}
    for f in tiles.glob("*.json"):
        for e in json.loads(f.read_text()):
            seen[(e["type"], e["id"])] = e
    out.write_text(json.dumps({"elements": list(seen.values())}))
    print(f"OSM ok ({len(seen)} prvků)")


OSM_QUERY = """[out:json][timeout:300];
(
  way["building"]({b});
  relation["building"]({b});
  way["highway"]({b});
  way["railway"~"rail|tram|light_rail"]({b});
  way["natural"~"water|wood|scrub|heath|grassland|wetland"]({b});
  relation["natural"~"water|wood"]({b});
  way["waterway"]({b});
  way["water"]({b});
  way["landuse"]({b});
  relation["landuse"]({b});
  way["leisure"~"park|garden|pitch|playground|golf_course|stadium|track"]({b});
  way["amenity"="parking"]({b});
  node["place"~"city|town|suburb|village|neighbourhood|quarter|hamlet|locality"]({b});
  nwr["tourism"~"attraction|museum|zoo|viewpoint"]["name"]({b});
  nwr["historic"]["name"]({b});
  nwr["amenity"~"place_of_worship|townhall|theatre"]["name"]({b});
);
out body geom;"""


OSM_EXTRA_QUERY = """[out:json][timeout:300];
( way["area:highway"]({b}); relation["area:highway"]({b});
  way["highway"]["area"="yes"]({b}); relation["highway"]["area"="yes"]({b});
  way["place"="square"]({b}); way["amenity"="parking"]({b}); relation["amenity"="parking"]({b});
  way["man_made"="bridge"]({b}); way["barrier"~"wall|fence|hedge|retaining_wall"]({b});
  node["highway"~"street_lamp|traffic_signals|stop|give_way|crossing|bus_stop"]({b});
);
out body geom;"""


def fetch_osm_extra():
    """Plochy silnic, parkoviště, bariéry, lampy, přechody, zastávky."""
    out = RAW / "osm_extra.json"
    if out.exists():
        return
    b = f'{BBOX["south"]},{BBOX["west"]},{BBOX["north"]},{BBOX["east"]}'
    out.write_text(json.dumps({"elements": overpass(OSM_EXTRA_QUERY.replace("{b}", b))}))


def main():
    for ds in FEEDS:
        sheets = sheets_in_bbox(fetch_feed(ds))
        print(f"{ds}: {len(sheets)} listů", flush=True)
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(lambda s: fetch_sheet(ds, s), sheets))
    fetch_osm()
    fetch_osm_extra()


if __name__ == "__main__":
    sys.exit(main())
