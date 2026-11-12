"""Vyhledávací index: adresy RÚIAN + ulice, místa a POI z OSM → web/public/data/search.json.

Formát: {"items": [[název, podtitul, druh, x, y, z], ...]} ve webových souřadnicích.
"""
import csv
import io
import json
import math
import zipfile
from collections import defaultdict

import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.ops import unary_union

from config import OUT, SHARED, write_regions
from process import T, build_dtm, dtm_sampler, element_polys, extent, load_osm, log

RUIAN_ZIP = SHARED / "ruian" / "adr.zip"


def ruian_addresses(ext):
    """Adresní místa ze všech obcí ČR, ořezaná na rozsah mapy."""
    x0, y0, x1, y1 = ext
    out = []
    with zipfile.ZipFile(RUIAN_ZIP) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            raw = zf.read(name).decode("cp1250")
            rows = csv.reader(io.StringIO(raw), delimiter=";")
            next(rows, None)
            for r in rows:
                try:
                    x, y = -float(r[16]), -float(r[17])  # S-JTSK Y,X → EPSG:5514
                except (ValueError, IndexError):
                    continue
                if not (x0 < x < x1 and y0 < y < y1):
                    continue
                obec, cast, ulice = r[2], r[8], r[10]
                cp = r[12] if r[11] == "č.p." else f"ev. {r[12]}"
                co = r[13] + r[14]
                num = f"{cp}/{co}" if co else cp
                label = f"{ulice or cast} {num}"
                sub = cast if cast == obec else f"{cast}, {obec}"
                # alternativní tvary pro hledání: "Chomutov 123", "čp 123", PSČ, část obce
                extra = f"{obec} {cast} {r[12]} {'čp' if r[11] == 'č.p.' else 'ev'} {r[12]} {r[15]} {r[15][:3]} {r[15][3:]}"
                out.append((label, f"{sub} · {r[15]}", "adresa", x, y, extra))
    return out


def osm_items(els):
    items = []
    streets = defaultdict(list)
    for e in els:
        tg = e.get("tags", {})
        name = tg.get("name")
        if not name:
            continue
        if "highway" in tg and e["type"] == "way" and "geometry" in e:
            pts = T.transform([p["lon"] for p in e["geometry"]], [p["lat"] for p in e["geometry"]])
            if len(pts[0]) >= 2:
                streets[name].append(LineString(list(zip(*pts))))
            continue
        if "place" in tg:
            kind = "místo"
        elif "building" in tg:
            kind = "budova"
        elif tg.get("natural") == "water" or "water" in tg or "waterway" in tg:
            kind = "voda"
        elif any(k in tg for k in ("leisure", "landuse", "natural")):
            kind = "oblast"
        else:
            kind = "místo zájmu"
        if e["type"] == "node":
            x, y = T.transform(e["lon"], e["lat"])
        else:
            g = element_polys(e)
            if g is None or g.is_empty:
                continue
            p = g.representative_point()
            x, y = p.x, p.y
        sub = tg.get("addr:city") or tg.get("tourism") or tg.get("amenity") or tg.get("historic") \
            or tg.get("leisure") or tg.get("place") or ""
        items.append((name, sub, kind, x, y, tg.get("addr:street", "")))
    # ulice: jeden výsledek na souvislý úsek se stejným názvem (různé obce = různé ulice)
    for name, lines in streets.items():
        merged = shapely.line_merge(unary_union(lines))
        parts = sorted(shapely.get_parts(merged), key=lambda g: -g.length)
        groups = []
        for part in parts:
            for g in groups:
                if g.distance(part) < 300:
                    groups[groups.index(g)] = g.union(part)
                    break
            else:
                groups.append(part)
        for g in groups:
            longest = max(shapely.get_parts(g), key=lambda q: q.length)
            p = longest.interpolate(0.5, normalized=True)
            items.append((name, "ulice", "ulice", p.x, p.y, ""))
    return items


def nearest_town(items):
    """Doplní k ulicím a POI název nejbližší obce/části."""
    towns = [(n, x, y) for n, s, k, x, y, _ in items if k == "místo" and s in ("town", "city", "village", "suburb")]
    if not towns:
        return items
    tx = np.array([t[1] for t in towns])
    ty = np.array([t[2] for t in towns])
    out = []
    for n, s, k, x, y, ex in items:
        if k in ("ulice", "místo zájmu", "budova", "oblast", "voda"):
            d = np.hypot(tx - x, ty - y)
            town = towns[int(d.argmin())][0]
            s = f"{s} · {town}" if s else town
        out.append((n, s, k, x, y, ex))
    return out


def main():
    ext = extent()
    dtm = build_dtm(ext)
    dtm_at = dtm_sampler(ext, dtm)
    z0 = math.floor(float(dtm.min()))
    ox, oy = (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2

    log("RÚIAN adresy")
    adr = ruian_addresses(ext)
    log(f"  {len(adr)} adres")
    log("OSM názvy")
    osm = nearest_town(osm_items(load_osm()))
    log(f"  {len(osm)} položek")

    seen, rows = set(), []
    for label, sub, kind, x, y, extra in osm + adr:
        key = (label, sub, kind, round(x / 5), round(y / 5))
        if key in seen:
            continue
        seen.add(key)
        rows.append((label, sub, kind, x, y, extra))
    xs = np.array([r[3] for r in rows])
    ys = np.array([r[4] for r in rows])
    zs = dtm_at(xs, ys) - z0
    items = [[r[0], r[1], r[2], round(r[3] - ox, 1), round(float(z), 1), round(oy - r[4], 1), r[5]]
             for r, z in zip(rows, zs)]
    (OUT / "search.json").write_text(json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":")))
    log(f"search.json: {len(items)} položek")
    write_regions()


if __name__ == "__main__":
    main()
