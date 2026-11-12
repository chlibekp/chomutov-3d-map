"""Zpracuje lidar + OSM do dlaždic pro web.

Výstup (web/public/data):
  meta.json                     – rozsah, počátek, seznam dlaždic
  terrain/{i}_{j}.bin           – Uint16 výšky (N+1)^2, (z - z0) * 20
  tex/{i}_{j}.jpg               – podkladová textura 1 m/px (land use, silnice, voda)
  buildings/{i}_{j}.bin         – mesh budov (pozice, uv, barvy)
  trees/{i}_{j}.bin             – Float32 [x, y, z, výška, poloměr, typ]
  labels.json                   – popisky
"""
import hashlib
import json
import math
import multiprocessing as mp
import os
import time

import laspy
import numpy as np
import shapely
from PIL import Image, ImageDraw
from pyproj import Transformer
from scipy import ndimage
from scipy.interpolate import griddata
from scipy.spatial import Delaunay, cKDTree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from config import BBOX, CHUNK, CITIES, CITY, CRS, OUT, RAW, START, TERRAIN_RES, WORK, write_regions

T = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
DTM_RES = 2.0
TERRAIN_N = int(CHUNK / TERRAIN_RES)
TEX_RES = 1.0


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- grid
class Grid:
    def __init__(self, ext, res):
        self.x0, self.y0, self.x1, self.y1 = ext
        self.res = res
        self.nx = int(round((self.x1 - self.x0) / res))
        self.ny = int(round((self.y1 - self.y0) / res))

    def cell(self, x, y):
        r = np.floor((self.y1 - np.asarray(y)) / self.res).astype(np.int64)
        c = np.floor((np.asarray(x) - self.x0) / self.res).astype(np.int64)
        return r, c

    def centers(self, r0, r1, c0, c1):
        xs = self.x0 + (np.arange(c0, c1) + 0.5) * self.res
        ys = self.y1 - (np.arange(r0, r1) + 0.5) * self.res
        return np.meshgrid(xs, ys)


def extent():
    xs, ys = T.transform(
        [BBOX["west"], BBOX["east"], BBOX["west"], BBOX["east"]],
        [BBOX["south"], BBOX["south"], BBOX["north"], BBOX["north"]],
    )
    return (
        math.floor(min(xs) / CHUNK) * CHUNK,
        math.floor(min(ys) / CHUNK) * CHUNK,
        math.ceil(max(xs) / CHUNK) * CHUNK,
        math.ceil(max(ys) / CHUNK) * CHUNK,
    )


# --------------------------------------------------------------------------- DTM
def read_xyz(f):
    las = laspy.read(f)
    return np.column_stack([las.x, las.y, las.z])


def build_dtm(ext):
    out = WORK / "dtm.npy"
    if out.exists():
        return np.load(out)
    g = Grid(ext, DTM_RES)
    log(f"DTM {g.nx}x{g.ny} @ {DTM_RES} m")
    files = sorted((RAW / "DMR5G").glob("*.laz"))
    allp = np.concatenate([read_xyz(f) for f in files])
    dtm = np.full((g.ny, g.nx), np.nan, np.float32)
    buf = 60.0
    step = 1000.0
    for bx in np.arange(g.x0, g.x1, step):
        for by in np.arange(g.y0, g.y1, step):
            m = ((allp[:, 0] > bx - buf) & (allp[:, 0] < bx + step + buf)
                 & (allp[:, 1] > by - buf) & (allp[:, 1] < by + step + buf))
            p = allp[m]
            if len(p) < 10:
                continue
            r0, c0 = g.cell(bx, by + step)
            r0, c0 = int(r0) + 0, int(c0)
            n = int(step / DTM_RES)
            r0 = max(r0, 0)
            gx, gy = g.centers(r0, r0 + n, c0, c0 + n)
            z = griddata(p[:, :2], p[:, 2], (gx, gy), method="linear")
            dtm[r0:r0 + n, c0:c0 + n] = z
    # díry (okraje mimo data) → nejbližší hodnota
    nan = np.isnan(dtm)
    if nan.any():
        idx = ndimage.distance_transform_edt(nan, return_distances=False, return_indices=True)
        dtm = dtm[tuple(idx)]
    np.save(out, dtm)
    return dtm


def dtm_sampler(ext, dtm):
    g = Grid(ext, DTM_RES)

    def sample(x, y):
        # bilineární, středy buněk
        fc = (np.asarray(x) - g.x0) / g.res - 0.5
        fr = (g.y1 - np.asarray(y)) / g.res - 0.5
        return ndimage.map_coordinates(dtm, [np.atleast_1d(fr), np.atleast_1d(fc)], order=1, mode="nearest")

    return sample


# --------------------------------------------------------------------------- DMP body
VEG_RES = 2.0


def build_surface(ext, dtm_at):
    """Body DMP1G: třída 6 (budovy) jako body s výškou nad terénem, třída 5 (vegetace) jako 2m rastr max."""
    fb, fv = WORK / "bld_pts.npy", WORK / "veg.npy"
    if fb.exists() and fv.exists():
        return np.load(fb), np.load(fv)
    gv = Grid(ext, VEG_RES)
    veg = np.full((gv.ny, gv.nx), np.nan, np.float32)
    bld = []
    for f in sorted((RAW / "DMP1G").glob("*.laz")):
        las = laspy.read(f)
        cls = np.asarray(las.classification)
        x, y, z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)
        m = (cls == 6) | (cls == 5)
        x, y, z, cls = x[m], y[m], z[m], cls[m]
        nz = z - dtm_at(x, y)
        mb = cls == 6
        bld.append(np.column_stack([x[mb], y[mb], nz[mb]]))
        mv = (cls == 5) & (nz > 0.5)
        r, c = gv.cell(x[mv], y[mv])
        ok = (r >= 0) & (r < gv.ny) & (c >= 0) & (c < gv.nx)
        np.fmax.at(veg, (r[ok], c[ok]), nz[mv][ok].astype(np.float32))
        log(f"  DMP {f.stem}")
    bld = np.concatenate(bld)
    np.save(fb, bld)
    np.save(fv, veg)
    return bld, veg


# --------------------------------------------------------------------------- OSM
def load_osm():
    data = json.loads((RAW / "osm.json").read_text())
    return data["elements"]


def proj_coords(geom):
    lon = [p["lon"] for p in geom if p]
    lat = [p["lat"] for p in geom if p]
    x, y = T.transform(lon, lat)
    return list(zip(x, y))


def element_polys(e):
    """Vrátí (Multi)Polygon pro way/relation, jinak None."""
    try:
        if e["type"] == "way":
            pts = proj_coords(e.get("geometry", []))
            if len(pts) < 4 or pts[0] != pts[-1]:
                return None
            p = Polygon(pts)
            return p if p.is_valid else p.buffer(0)
        if e["type"] == "relation":
            outer, inner = [], []
            for m in e.get("members", []):
                if m.get("type") != "way" or "geometry" not in m:
                    continue
                ls = LineString(proj_coords(m["geometry"]))
                (inner if m.get("role") == "inner" else outer).append(ls)
            o = unary_union(list(polygonize(unary_union(outer)))) if outer else None
            if o is None or o.is_empty:
                return None
            if inner:
                o = o.difference(unary_union(list(polygonize(unary_union(inner)))))
            return o
    except Exception:  # noqa: BLE001
        return None
    return None


def element_line(e):
    if e["type"] != "way" or "geometry" not in e:
        return None
    pts = proj_coords(e["geometry"])
    return LineString(pts) if len(pts) >= 2 else None


def h32(s):
    return int(hashlib.md5(str(s).encode()).hexdigest()[:8], 16)


# --------------------------------------------------------------------------- budovy
WALL_COLORS = [
    (214, 200, 172), (196, 186, 170), (226, 214, 190), (186, 172, 152),
    (205, 190, 160), (170, 168, 165), (220, 205, 180), (190, 160, 135),
    (215, 180, 150), (178, 182, 186),
]
ROOF_PITCHED = [
    (150, 72, 52), (135, 66, 50), (160, 82, 58), (122, 60, 48), (142, 78, 62),
    (118, 72, 62), (168, 92, 64), (110, 58, 50), (98, 82, 78), (130, 70, 60),
]
ROOF_FLAT = [(120, 118, 115), (105, 104, 102), (135, 132, 126), (92, 92, 94), (140, 128, 116)]


class MeshBuilder:
    def __init__(self):
        self.pos, self.uv, self.col = [], [], []
        self.bulk = []  # (pozice (n,3) float32, barva)

    def tri(self, a, b, c, uva, uvb, uvc, col):
        self.pos += [a, b, c]
        self.uv += [uva, uvb, uvc]
        self.col += [col, col, col]

    def tris_bulk(self, P, col):
        if len(P):
            self.bulk.append((np.asarray(P, np.float32).reshape(-1, 3), col))

    def count(self):
        return len(self.pos) + sum(len(b[0]) for b in self.bulk)

    def bytes(self):
        n = self.count()
        head = np.array([n], np.uint32).tobytes()
        P = np.concatenate([np.asarray(self.pos, np.float32).reshape(-1, 3)] + [b[0] for b in self.bulk])
        U = np.concatenate([np.asarray(self.uv, np.float32).reshape(-1, 2)]
                           + [np.full((len(b[0]), 2), -1, np.float32) for b in self.bulk])
        C = np.full((n, 4), 255, np.uint8)
        k = len(self.pos)
        if k:
            C[:k, :3] = np.asarray(self.col, np.uint8)
        for bp, col in self.bulk:
            C[k:k + len(bp), :3] = col
            k += len(bp)
        return head + P.tobytes() + U.tobytes() + C.tobytes()


def building_params(poly, tags, bpts, btree, edge):
    """Střecha jako z = okap + k * vzdálenost_od_obvodu (straight-skeleton aproximace).

    k a okap se fitují regresí lidarových bodů střechy (třída 6). Vrací (okap, hřeben, k)."""
    levels = tags.get("building:levels")
    try:
        levels = float(str(levels).split(";")[0].replace(",", "."))
    except (TypeError, ValueError):
        levels = None
    area = poly.area
    c = poly.centroid
    rad = max(np.hypot(*(np.asarray(poly.exterior.coords) - [c.x, c.y]).T)) + 0.5
    idx = btree.query_ball_point([c.x, c.y], rad)
    pts = bpts[idx] if idx else np.zeros((0, 3))
    if len(pts):
        inner = poly.buffer(-0.3)
        pts = pts[shapely.contains_xy(inner if not inner.is_empty else poly, pts[:, 0], pts[:, 1])]
        pts = pts[pts[:, 2] > 1.5]
    shape = tags.get("roof:shape")
    if len(pts) < 3:
        h = (levels * 3.0 + 1.0) if levels else (3.5 if area < 40 else 7.0)
        if area < 600 and shape != "flat":
            return h, h + 4.0, 0.75
        return h, h, 0.0
    z = pts[:, 2]
    top_obs = float(np.percentile(z, 96))
    med = float(np.median(z))
    if len(pts) >= 5 and shape != "flat" and area < 15000:
        dist = shapely.distance(edge, shapely.points(pts[:, :2]))
        # model: z = min(okap + k*d, hřeben); sklon jen z bodů pod hřebenem
        sl = z < top_obs - 0.4
        if sl.sum() >= 4 and dist[sl].std() > 0.3:
            A = np.column_stack([np.ones(sl.sum()), dist[sl]])
            (a0, k), *_ = np.linalg.lstsq(A, z[sl], rcond=None)
            pred = np.minimum(a0 + k * dist, top_obs)
            mad_p = np.median(np.abs(z - pred))
            mad_f = np.median(np.abs(z - med))
            if 0.25 < k < 2.5 and mad_p < 0.75 * mad_f and top_obs - a0 > 0.8:
                eave = max(float(a0), 2.5)
                return eave, max(top_obs, eave + 0.8), float(k)
    if shape in ("gabled", "hipped", "pitched"):
        eave = max(float(np.percentile(z, 15)), 2.5)
        return eave, max(top_obs, eave + 1.0), 0.75
    h = max(med, 2.5)
    return h, h, 0.0


def tri_polys(poly):
    tris = shapely.constrained_delaunay_triangles(poly)
    out = []
    for t in shapely.get_parts(tris):
        cs = np.asarray(t.exterior.coords)[:3]
        a = (cs[1, 0] - cs[0, 0]) * (cs[2, 1] - cs[0, 1]) - (cs[1, 1] - cs[0, 1]) * (cs[2, 0] - cs[0, 0])
        if abs(a) < 1e-6:
            continue
        out.append(cs if a > 0 else cs[::-1])
    return out


def roof_tris(poly, edge, eave, top, k, step=1.6):
    """Triangulace s vnitřními body; výška = min(okap + k*d, hřeben)."""
    bnd = np.asarray(shapely.segmentize(poly, step).exterior.coords)[:-1]
    rings = [bnd] + [np.asarray(shapely.segmentize(shapely.Polygon(h), step).exterior.coords)[:-1]
                     for h in poly.interiors]
    minx, miny, maxx, maxy = poly.bounds
    gx, gy = np.meshgrid(np.arange(minx + step / 2, maxx, step), np.arange(miny + step / 2, maxy, step))
    gx, gy = gx.ravel(), gy.ravel()
    inner = poly.buffer(-step * 0.45)
    m = shapely.contains_xy(inner, gx, gy) if not inner.is_empty else np.zeros(len(gx), bool)
    P = np.concatenate(rings + [np.column_stack([gx[m], gy[m]])])
    P = np.unique(np.round(P, 3), axis=0)
    if len(P) < 3:
        return [], None
    d = shapely.distance(edge, shapely.points(P))
    Z = np.minimum(eave + k * d, top)
    tri = Delaunay(P).simplices
    cen = P[tri].mean(axis=1)
    keep = shapely.contains_xy(poly.buffer(0.05), cen[:, 0], cen[:, 1])
    out = []
    for t in tri[keep]:
        a, b_, c_ = P[t]
        ar = (b_[0] - a[0]) * (c_[1] - a[1]) - (b_[1] - a[1]) * (c_[0] - a[0])
        if abs(ar) < 1e-6:
            continue
        out.append(t if ar > 0 else t[::-1])
    return out, (P, Z)


def add_building(mb, poly, edge, base, eave, top, k, wall_col, roof_col, W, step=1.6):
    """W(x, y) -> (wx, wz). Y nahoru. `edge` = volné hrany (okapy); u sdílených zdí roste štít."""
    poly = shapely.geometry.polygon.orient(poly, 1.0)
    if k > 0:
        tris, (P, Z) = roof_tris(poly, edge, eave, top, k, step)
        for t in tris:
            V = [(*W(*P[q])[:1], base + Z[q], W(*P[q])[1]) for q in t]
            mb.tri(V[0], V[1], V[2], (-1, 0), (-1, 0), (-1, 0), roof_col)
    else:
        for t in tri_polys(poly):
            V = [(W(x, y)[0], base + top, W(x, y)[1]) for x, y in t]
            mb.tri(V[0], V[1], V[2], (-1, 0), (-1, 0), (-1, 0), roof_col)
    for ring in [poly.exterior, *poly.interiors]:
        cs = np.asarray(shapely.segmentize(shapely.LinearRing(ring.coords), step).coords) if k > 0 \
            else np.asarray(ring.coords)
        if k > 0:
            zt = np.minimum(eave + k * shapely.distance(edge, shapely.points(cs)), top)
        else:
            zt = np.full(len(cs), top)
        u = 0.0
        u_off = (cs[0, 0] * 0.37 + cs[0, 1] * 0.11) % 50  # náhodný posun oken
        for q in range(len(cs) - 1):
            (ax, ay), (bx, by) = cs[q], cs[q + 1]
            seg = math.hypot(bx - ax, by - ay)
            if seg < 1e-3:
                continue
            za, zb = float(zt[q]), float(zt[q + 1])
            wa, wb = W(ax, ay), W(bx, by)
            A0, B0 = (wa[0], base, wa[1]), (wb[0], base, wb[1])
            A1, B1 = (wa[0], base + za, wa[1]), (wb[0], base + zb, wb[1])
            u0 = u_off + u
            mb.tri(A0, B0, B1, (u0, 0), (u0 + seg, 0), (u0 + seg, zb), wall_col)
            mb.tri(A0, B1, A1, (u0, 0), (u0 + seg, zb), (u0, za), wall_col)
            u += seg


# --------------------------------------------------------------------------- textura
LANDUSE_COL = {
    "residential": (138, 146, 128), "commercial": (146, 146, 138), "retail": (146, 146, 138),
    "industrial": (150, 148, 140), "railway": (140, 136, 128), "construction": (160, 150, 130),
    "farmland": (150, 160, 106), "meadow": (126, 156, 98), "grass": (120, 154, 100),
    "village_green": (120, 154, 100), "recreation_ground": (118, 152, 96),
    "allotments": (128, 150, 96), "orchard": (118, 146, 90), "vineyard": (128, 146, 92),
    "forest": (82, 112, 70), "cemetery": (112, 136, 96), "quarry": (170, 160, 140),
    "landfill": (160, 150, 130), "brownfield": (156, 150, 128), "garages": (150, 148, 144),
}
NATURAL_COL = {
    "wood": (82, 112, 70), "scrub": (104, 130, 82), "heath": (130, 140, 90),
    "grassland": (126, 156, 98), "wetland": (100, 130, 110), "water": (74, 108, 150),
}
LEISURE_COL = {
    "park": (112, 148, 92), "garden": (118, 150, 94), "pitch": (110, 150, 100),
    "playground": (150, 150, 120), "golf_course": (120, 164, 100), "stadium": (130, 140, 120),
    "track": (170, 120, 100),
}
ROAD_W = {
    "motorway": 14, "trunk": 12, "primary": 10, "secondary": 9, "tertiary": 8,
    "motorway_link": 7, "trunk_link": 7, "primary_link": 7, "secondary_link": 6, "tertiary_link": 6,
    "residential": 6.5, "unclassified": 5.5, "living_street": 5.5, "service": 4,
    "pedestrian": 5, "track": 3, "footway": 2.2, "path": 1.8, "cycleway": 2.2, "steps": 2,
    "bridleway": 2,
}
MAJOR = {"motorway", "trunk", "primary", "secondary"}
GROUND = (120, 150, 96)
WATER = (74, 108, 150)


_G = {}  # sdílená data pro fork workery


def _pool():
    return mp.get_context("fork").Pool(os.cpu_count())


class Layer:
    """Geometrie + STRtree pro rychlý výběr podle dlaždice."""

    def __init__(self, items, geom_of):
        self.items = items
        self.tree = shapely.STRtree([geom_of(it) for it in items]) if items else None

    def query(self, bb):
        if self.tree is None:
            return []
        return [self.items[k] for k in sorted(self.tree.query(bb, predicate="intersects"))]


def _tex_chunk(ij):
    i, j = ij
    g = _G["tex_grid"]
    SS = 2  # supersampling → vyhlazené hrany
    px = int(CHUNK / TEX_RES)
    cx0 = g.x0 + i * CHUNK
    cy1 = g.y1 - j * CHUNK
    img = Image.new("RGB", (px * SS, px * SS), GROUND)
    dr = ImageDraw.Draw(img)
    bb = shapely.box(cx0, cy1 - CHUNK, cx0 + CHUNK, cy1)
    sc = SS / TEX_RES

    def P(coords):
        return [((x - cx0) * sc, (cy1 - y) * sc) for x, y in coords]

    def fill(geom, col):
        for p in shapely.get_parts(geom):
            if p.geom_type != "Polygon" or p.is_empty:
                continue
            dr.polygon(P(p.exterior.coords), fill=col)
            for h in p.interiors:
                dr.polygon(P(h.coords), fill=GROUND)

    for geom, col in _G["land"].query(bb):
        fill(geom, col)
    for ln, w in _G["waterways"].query(bb):
        dr.line(P(ln.coords), fill=WATER, width=max(1, int(w * sc)), joint="curve")
    for geom in _G["water"].query(bb):
        fill(geom, WATER)
    # detail: silnice se kreslí jako 3D geometrie, v textuře jen jemný podklad
    img.resize((px, px), Image.LANCZOS).save(OUT / "tex" / f"{i}_{j}.jpg", quality=85)
    # vzdálený pohled: silnice zapečené do textury
    for ln, w, kind, _ in _G["roads"].query(bb):
        col = (228, 220, 196) if kind in MAJOR else (200, 200, 194) if w >= 4 else (214, 208, 190)
        if w >= 4:
            dr.line(P(ln.coords), fill=(150, 150, 146), width=int((w + 1.2) * sc), joint="curve")
        dr.line(P(ln.coords), fill=col, width=max(1, int(w * sc)), joint="curve")
    for ln, _ in _G["rails"].query(bb):
        dr.line(P(ln.coords), fill=(110, 104, 98), width=int(3 * sc), joint="curve")
    img.resize((256, 256), Image.LANCZOS).save(OUT / "tex_lo" / f"{i}_{j}.jpg", quality=80)


def render_textures(ext, polys_land, water_polys, waterways, roads, rails, chunks):
    (OUT / "tex").mkdir(exist_ok=True)
    (OUT / "tex_lo").mkdir(exist_ok=True)
    # pořadí kreslení se zachová: query vrací indexy seřazené
    _G.update(
        tex_grid=Grid(ext, TEX_RES),
        land=Layer(polys_land, lambda t: t[0]),
        water=Layer(water_polys, lambda t: t),
        waterways=Layer(waterways, lambda t: t[0]),
        roads=Layer(sorted(roads, key=lambda r: r[1]), lambda t: t[0]),
        rails=Layer(rails, lambda t: t[0]),
    )
    with _pool() as pool:
        pool.map(_tex_chunk, chunks, chunksize=2)


def _bld_chunk(args):
    ch, items = args
    mb = MeshBuilder()
    bpts, btree, dtm_at, z0, W = _G["bpts"], _G["btree"], _G["dtm_at"], _G["z0"], _G["W"]
    allp, ptree = _G["allpolys"], _G["ptree"]
    for oid, poly, tg in items:
        # volné hrany = obvod mínus části sdílené se sousedními budovami
        bnd = poly.boundary
        nb = [allp[q] for q in ptree.query(poly.buffer(0.6), predicate="intersects") 
              if not shapely.equals_exact(allp[q], poly, 1e-6)]
        edge = bnd
        if nb:
            free = bnd.difference(unary_union(nb).buffer(0.6))
            if not free.is_empty and free.length > 0.15 * bnd.length:
                edge = free
        eave, top, k = building_params(poly, tg, bpts, btree, edge)
        ring = np.asarray(poly.exterior.coords)
        base = float(dtm_at(ring[:, 0], ring[:, 1]).min()) - z0 - 0.3
        hh = h32(oid)
        wall = WALL_COLORS[hh % len(WALL_COLORS)]
        pal = ROOF_PITCHED if k > 0 else ROOF_FLAT
        roof = pal[(hh >> 8) % len(pal)]
        try:
            add_building(mb, poly, edge, base, eave, top, k, wall, roof, W)
        except Exception as ex:  # noqa: BLE001
            print("  chyba budovy", oid, ex, flush=True)
    if mb.pos:
        (OUT / "buildings" / f"{ch[0]}_{ch[1]}.bin").write_bytes(mb.bytes())
    return ch, len(mb.pos)


# --------------------------------------------------------------------------- 3D silnice
ROAD_STYLE = {
    # druh: (barva povrchu, barva krajnice | None, výška nad terénem)
    "major": ((184, 182, 176), (120, 120, 116), 0.30),
    "road": ((176, 175, 170), (124, 124, 120), 0.28),
    "service": ((168, 167, 162), None, 0.26),
    "pedestrian": ((196, 190, 176), None, 0.26),
    "footway": ((206, 200, 184), None, 0.24),
    "path": ((176, 162, 128), None, 0.22),
    "track": ((150, 134, 104), None, 0.21),
    "steps": ((186, 180, 168), None, 0.25),
    "cycleway": ((172, 120, 104), None, 0.24),
    "parking": ((140, 140, 137), None, 0.25),
}
MARK = (236, 234, 226)
SIDEWALK = (206, 202, 192)


def road_class(kind, sub):
    if kind in ("motorway", "trunk", "primary", "secondary", "motorway_link", "trunk_link",
                "primary_link", "secondary_link"):
        return "major"
    if kind in ("tertiary", "tertiary_link", "residential", "unclassified", "living_street"):
        return "road"
    if kind == "service":
        return "parking" if sub in ("parking_aisle",) else "service"
    if kind == "pedestrian":
        return "pedestrian"
    if kind in ("footway", "bridleway"):
        return "footway"
    if kind == "cycleway":
        return "cycleway"
    if kind == "track":
        return "track"
    if kind == "steps":
        return "steps"
    return "path"


def terrain_mesh_sampler(ext, dtm_at, z0):
    """Výška přesně podle trojúhelníků terénního meshe ve webu (4m mřížka, kvantizace 1/20 m)."""
    nxv = int((ext[2] - ext[0]) / TERRAIN_RES) + 1
    nyv = int((ext[3] - ext[1]) / TERRAIN_RES) + 1
    xs = ext[0] + np.arange(nxv) * TERRAIN_RES
    out = np.empty((nyv, nxv), np.float32)
    for k in range(nyv):
        y = ext[3] - k * TERRAIN_RES
        out[k] = np.round(np.clip((dtm_at(xs, np.full(nxv, y)) - z0) * 20, 0, 65535)) / 20

    def sample(x, y):
        fl = (np.asarray(x) - ext[0]) / TERRAIN_RES
        fk = (ext[3] - np.asarray(y)) / TERRAIN_RES
        l = np.clip(np.floor(fl).astype(int), 0, nxv - 2)
        k = np.clip(np.floor(fk).astype(int), 0, nyv - 2)
        fx, fz = fl - l, fk - k
        a, b = out[k, l], out[k + 1, l]
        c, d = out[k + 1, l + 1], out[k, l + 1]
        t1 = a + (d - a) * fx + (b - a) * fz
        t2 = c + (b - c) * (1 - fx) + (d - c) * (1 - fz)
        return np.where(fx + fz <= 1, t1, t2)

    return sample


class Ribbon:
    """Pomocník pro plochou geometrii nad terénem (vrcholy v S-JTSK + výška)."""

    def __init__(self, mb, W):
        self.mb, self.W = mb, W

    def tri(self, p, q, r, col):
        (x1, y1, _), (x2, y2, _), (x3, y3, _) = p, q, r
        if (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1) < 0:
            q, r = r, q
        V = [(self.W(x, y)[0], z, self.W(x, y)[1]) for x, y, z in (p, q, r)]
        self.mb.tri(V[0], V[1], V[2], (-1, 0), (-1, 0), (-1, 0), col)

    def quad(self, a, b, c, d, col):  # a-b-c-d po obvodu
        self.tri(a, b, c, col)
        self.tri(a, c, d, col)


def offsets(pts, half):
    """Levý/pravý okraj polyline s miter spoji."""
    d = np.diff(pts, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])[:, None]
    d = d / np.maximum(L, 1e-9)
    nseg = np.column_stack([-d[:, 1], d[:, 0]])
    n = np.zeros_like(pts)
    n[:-1] += nseg
    n[1:] += nseg
    n /= np.maximum(np.hypot(n[:, 0], n[:, 1])[:, None], 1e-9)
    # miter délka
    cosang = np.ones(len(pts))
    cosang[:-1] = np.minimum(cosang[:-1], np.abs((n[:-1] * nseg).sum(1)))
    cosang[1:] = np.minimum(cosang[1:], np.abs((n[1:] * nseg).sum(1)))
    m = half / np.maximum(cosang, 0.5)
    return pts + n * m[:, None], pts - n * m[:, None]


def strip(rb, pts, z, half, col):
    left, right = offsets(pts, half)
    for q in range(len(pts) - 1):
        rb.quad((*left[q], z[q]), (*left[q + 1], z[q + 1]), (*right[q + 1], z[q + 1]), (*right[q], z[q]), col)


def disc(rb, x, y, z, r, col, n=10):
    ang = np.linspace(0, 2 * np.pi, n + 1)
    for q in range(n):
        rb.tri((x, y, z), (x + r * np.cos(ang[q]), y + r * np.sin(ang[q]), z),
               (x + r * np.cos(ang[q + 1]), y + r * np.sin(ang[q + 1]), z), col)


def line_z(ln, pts, bridge, tz, lift):
    if bridge:
        # most: lineárně mezi konci, ne podle údolí
        s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
        e0, e1 = tz(*ln.coords[0]), tz(*ln.coords[-1])
        return (e0 + (e1 - e0) * s / max(s[-1], 1e-6)) + lift + 0.4
    return tz(pts[:, 0], pts[:, 1]) + lift


def dashes(rb, pts, z, width, dash, gap, col, lift=0.03):
    """Přerušovaná čára podél polyline."""
    s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
    if s[-1] < dash:
        return
    for t0 in np.arange(gap / 2, s[-1] - dash, dash + gap):
        tt = np.array([t0, t0 + dash])
        xs, ys, zs = np.interp(tt, s, pts[:, 0]), np.interp(tt, s, pts[:, 1]), np.interp(tt, s, z) + lift
        strip(rb, np.column_stack([xs, ys]), zs, width / 2, col)


def grid_split(geom, size):
    """Rozřeže (Multi)Polygon mřížkou → pole polygonů."""
    if geom is None or geom.is_empty:
        return np.array([], dtype=object)
    minx, miny, maxx, maxy = geom.bounds
    xs = np.arange(math.floor(minx / size) * size, maxx, size)
    ys = np.arange(math.floor(miny / size) * size, maxy, size)
    bx, by = np.meshgrid(xs, ys)
    boxes = shapely.box(bx.ravel(), by.ravel(), bx.ravel() + size, by.ravel() + size)
    shapely.prepare(geom)
    boxes = boxes[shapely.intersects(boxes, geom)]
    pieces = shapely.get_parts(shapely.intersection(boxes, geom))
    pieces = pieces[shapely.get_type_id(pieces) == 3]
    return pieces[shapely.area(pieces) > 0.02]


def drape(geom, tz, lift, cell=8.0):
    """Polygon → trojúhelníky (N,3,3) [x,y,z] přesně kopírující terénní mesh."""
    if geom is None or geom.is_empty:
        return np.zeros((0, 3, 3))
    geom = shapely.make_valid(geom)
    fine = [grid_split(c, cell) for c in grid_split(geom, 96.0)]
    fine = np.concatenate(fine) if fine else np.array([], dtype=object)
    if not len(fine):
        return np.zeros((0, 3, 3))
    fine = shapely.segmentize(shapely.set_precision(fine, 0.005), 4.0)
    fine = fine[(~shapely.is_empty(fine)) & (shapely.get_type_id(fine) == 3)]
    try:
        tris = shapely.get_parts(shapely.constrained_delaunay_triangles(fine))
    except shapely.errors.GEOSException:
        # degenerované kousky po jednom; problematické vynechat
        parts = []
        for f in fine:
            try:
                parts.append(shapely.get_parts(shapely.constrained_delaunay_triangles(shapely.make_valid(f))))
            except shapely.errors.GEOSException:
                continue
        tris = np.concatenate(parts) if parts else np.array([], dtype=object)
    tris = tris[shapely.get_type_id(tris) == 3] if len(tris) else tris
    if not len(tris):
        return np.zeros((0, 3, 3))
    xy = shapely.get_coordinates(tris).reshape(-1, 4, 2)[:, :3]
    z = tz(xy[..., 0].ravel(), xy[..., 1].ravel()).reshape(-1, 3) + lift
    return np.concatenate([xy, z[..., None]], axis=-1)


def emit(mb, T, W, col):
    """Trojúhelníky (N,3,3) v S-JTSK → mesh, otočené normálou nahoru."""
    if not len(T):
        return
    a = ((T[:, 1, 0] - T[:, 0, 0]) * (T[:, 2, 1] - T[:, 0, 1])
         - (T[:, 1, 1] - T[:, 0, 1]) * (T[:, 2, 0] - T[:, 0, 0]))
    T = T[np.abs(a) > 1e-6]
    a = a[np.abs(a) > 1e-6]
    T[a < 0] = T[a < 0][:, [0, 2, 1]]
    wx, wz = W(T[..., 0], T[..., 1])
    mb.tris_bulk(np.stack([wx, T[..., 2], wz], -1), col)


def vwalls(mb, A, B, za0, zb0, za1, zb1, W, col, both=False):
    """Svislé stěny nad úsečkami A→B (N,2); normála vpravo od směru (u ccw obvodu ven)."""
    if not len(A):
        return
    ax, az = W(A[:, 0], A[:, 1])
    bx, bz = W(B[:, 0], B[:, 1])
    A0 = np.stack([ax, za0, az], -1)
    B0 = np.stack([bx, zb0, bz], -1)
    A1 = np.stack([ax, za1, az], -1)
    B1 = np.stack([bx, zb1, bz], -1)
    T = np.concatenate([np.stack([A0, B0, B1], 1), np.stack([A0, B1, A1], 1)])
    mb.tris_bulk(T, col)
    if both:
        mb.tris_bulk(T[:, [0, 2, 1]], col)


def ring_segments(geom, step=2.0):
    """Všechny hrany obvodů (vnější ccw, díry cw) jako pole úseček A, B."""
    A, B = [], []
    for p in shapely.get_parts(geom):
        if p.geom_type != "Polygon":
            continue
        p = shapely.geometry.polygon.orient(p, 1.0)
        for r in [p.exterior, *p.interiors]:
            c = np.asarray(shapely.segmentize(shapely.LinearRing(r.coords), step).coords)
            A.append(c[:-1])
            B.append(c[1:])
    if not A:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.concatenate(A), np.concatenate(B)


def line_segments(geom, step=2.0):
    A, B = [], []
    for ln in shapely.get_parts(geom):
        if ln.geom_type != "LineString" or ln.length < 0.1:
            continue
        c = np.asarray(shapely.segmentize(ln, step).coords)
        A.append(c[:-1])
        B.append(c[1:])
    if not A:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.concatenate(A), np.concatenate(B)


def thin_box_along(mb, geom, half, h, base_lift, tz, W, col):
    """Zeď/plot/živý plot podél linie: dvě strany + vršek."""
    for ln in shapely.get_parts(geom):
        if ln.geom_type != "LineString" or ln.length < 0.3:
            continue
        pts = np.asarray(shapely.segmentize(ln, 2.0).coords)
        z = tz(pts[:, 0], pts[:, 1]) + base_lift
        L, R = offsets(pts, half)
        vwalls(mb, R[:-1], R[1:], z[:-1], z[1:], z[:-1] + h, z[1:] + h, W, col, both=True)
        vwalls(mb, L[1:], L[:-1], z[1:], z[:-1], z[1:] + h, z[:-1] + h, W, col, both=True)
        rb = Ribbon(mb, W)
        strip(rb, pts, z + h, half, col)


CAR = ("major", "road", "service", "parking")
ASPHALT = (166, 165, 160)
ASPHALT_MAJOR = (158, 157, 153)
PAVING = (198, 192, 178)
PARKING_COL = (156, 155, 151)
CURB = (214, 211, 202)
FOOT_COL = {"footway": (204, 198, 182), "cycleway": (176, 122, 106), "path": (172, 158, 124),
            "steps": (190, 184, 172), "pedestrian": PAVING}
LIFT = dict(parking=0.22, car=0.25, foot=0.28, ped=0.30, sidewalk=0.37, mark=0.28)
SW_W = 2.2  # šířka chodníku (m)


def _road_chunk(ij):
    i, j = ij
    g, tz, W = _G["tex_grid"], _G["tz"], _G["W"]
    cx0, cy1 = g.x0 + i * CHUNK, g.y1 - j * CHUNK
    bb = shapely.box(cx0, cy1 - CHUNK, cx0 + CHUNK, cy1)
    bbx = bb.buffer(40, join_style="mitre")
    mb = MeshBuilder()
    rb = Ribbon(mb, W)
    items = _G["roads"].query(bbx)

    def clip(geom):
        return None if geom is None or geom.is_empty else geom.intersection(bb)

    def buf(ln, r):
        return ln.buffer(r, quad_segs=4)

    car, foot, tracks, bridges, crossings = [], [], [], [], []
    for it in items:
        ln, w, kind, at = it
        cls = road_class(kind, at["sub"])
        if at["sub"] == "crossing":
            crossings.append(it)
        elif at["bridge"]:
            bridges.append(it)
        elif cls == "track":
            tracks.append(it)
        elif cls in CAR:
            car.append(it)
        else:
            foot.append(it)

    # --- plochy
    car_major = unary_union([buf(ln, w / 2) for ln, w, k, at in car if road_class(k, at["sub"]) == "major"])
    carriage = unary_union([buf(ln, w / 2) for ln, w, k, at in car]
                           + [p for p, kind in _G["areas"].query(bbx) if kind == "car"])
    ped = unary_union([buf(ln, w / 2) for ln, w, k, at in foot if road_class(k, at["sub"]) == "pedestrian"]
                      + [p for p, kind in _G["areas"].query(bbx) if kind == "ped"])
    if not ped.is_empty:
        ped = ped.difference(carriage)
    bld = unary_union(_G["allpolys_layer"].query(bbx))
    sw = unary_union([buf(ln, w / 2 + SW_W) for ln, w, k, at in car if at.get("sidewalk")])
    if not sw.is_empty:
        sw = sw.difference(carriage).difference(bld)
        if not ped.is_empty:
            sw = sw.difference(ped)
    parking = unary_union(_G["parking"].query(bbx))
    if not parking.is_empty:
        parking = parking.difference(carriage).difference(bld)

    emit(mb, drape(clip(parking), tz, LIFT["parking"]), W, PARKING_COL)
    emit(mb, drape(clip(carriage.difference(car_major) if not car_major.is_empty else carriage), tz, LIFT["car"]), W, ASPHALT)
    emit(mb, drape(clip(car_major), tz, LIFT["car"]), W, ASPHALT_MAJOR)
    emit(mb, drape(clip(ped), tz, LIFT["ped"]), W, PAVING)
    for cls in ("path", "cycleway", "footway", "steps"):
        fp = unary_union([buf(ln, w / 2) for ln, w, k, at in foot if road_class(k, at["sub"]) == cls])
        if not fp.is_empty:
            fp = fp.difference(carriage).difference(bld)
            emit(mb, drape(clip(fp), tz, LIFT["foot"] + (0.01 if cls == "footway" else 0)), W, FOOT_COL[cls])
    sw_c = clip(sw)
    emit(mb, drape(sw_c, tz, LIFT["sidewalk"]), W, PAVING)

    # --- obrubníky: hrany chodníku, které sousedí s vozovkou
    if sw_c is not None and not sw_c.is_empty and not carriage.is_empty:
        A, B = ring_segments(sw_c, 2.0)
        if len(A):
            mid = (A + B) / 2
            near = shapely.distance(shapely.boundary(carriage), shapely.points(mid)) < 0.2
            A, B = A[near], B[near]
            za, zb = tz(A[:, 0], A[:, 1]), tz(B[:, 0], B[:, 1])
            # stěna orientovaná ven z chodníku (k vozovce)
            vwalls(mb, A, B, za + LIFT["car"], zb + LIFT["car"], za + LIFT["sidewalk"], zb + LIFT["sidewalk"], W, CURB)

    # --- vodorovné značení
    zl = LIFT["car"] + 0.03
    for ln, w, kind, at in car:
        cls = road_class(kind, at["sub"])
        for part in shapely.get_parts(ln.intersection(bb)):
            if part.geom_type != "LineString" or part.length < 1:
                continue
            pts = np.asarray(shapely.segmentize(part, 2.0).coords)
            z = tz(pts[:, 0], pts[:, 1]) + zl
            lanes = int(at["lanes"] or (2 if cls == "major" and not at["oneway"] else 1))
            if cls == "major" or (kind == "tertiary" and w >= 6):
                if not at["oneway"]:
                    if lanes >= 4:
                        for o in (-0.12, 0.12):
                            strip(rb, offsets(pts, o)[0], z, 0.06, MARK)
                    else:
                        dashes(rb, pts, z, 0.12, 3.0, 3.0 if cls == "major" else 6.0, MARK, lift=0)
                    half_l = lanes / 2
                    for q in range(1, int(half_l)):
                        off = w / 2 * q / half_l
                        for side in (0, 1):
                            dashes(rb, offsets(pts, off)[side], z, 0.1, 3.0, 6.0, MARK, lift=0)
                elif lanes >= 2:
                    for q in range(1, lanes):
                        off = -w / 2 + w * q / lanes
                        o = offsets(pts, abs(off))[0 if off > 0 else 1]
                        dashes(rb, o, z, 0.1, 3.0, 6.0, MARK, lift=0)
            if cls == "major":
                # okrajové čáry přerušené u odboček
                others = unary_union([buf(l2, w2 / 2 + 1.0) for l2, w2, k2, a2 in car
                                      if l2 is not ln and l2.intersects(buf(ln, w + 2))])
                for side in (0, 1):
                    edge = LineString(offsets(pts, w / 2 - 0.35)[side])
                    if not others.is_empty:
                        edge = edge.difference(others)
                    for e in shapely.get_parts(edge):
                        if e.geom_type == "LineString" and e.length > 1:
                            ep = np.asarray(shapely.segmentize(e, 2.0).coords)
                            strip(rb, ep, tz(ep[:, 0], ep[:, 1]) + zl, 0.07, MARK)

    # --- přechody: cesty (footway=crossing) a uzly highway=crossing
    def zebra(cx, cy, dx, dy, length, z_of):
        # (dx,dy) = směr chůze (napříč silnicí); pruhy po 1 m
        nx, ny = -dy, dx
        for t in np.arange(-length / 2 + 0.25, length / 2 - 0.3, 1.0):
            c0 = np.array([cx + dx * t, cy + dy * t])
            c1 = c0 + np.array([dx, dy]) * 0.5
            corners = [c0 - [nx * 1.5, ny * 1.5], c1 - [nx * 1.5, ny * 1.5], c1 + [nx * 1.5, ny * 1.5], c0 + [nx * 1.5, ny * 1.5]]
            zz = [z_of(*c) for c in corners]
            rb.quad(*[(c[0], c[1], zc) for c, zc in zip(corners, zz)], MARK)

    zz_of = lambda x, y: float(tz(np.array([x]), np.array([y]))[0]) + zl + 0.01  # noqa: E731
    done = []
    for ln, w, kind, at in crossings:
        part = ln.intersection(bb)
        for pc in shapely.get_parts(part):
            if pc.geom_type != "LineString" or pc.length < 1:
                continue
            a, b2 = np.asarray(pc.coords[0]), np.asarray(pc.coords[-1])
            d = (b2 - a) / max(np.hypot(*(b2 - a)), 1e-6)
            m = (a + b2) / 2
            zebra(m[0], m[1], d[0], d[1], pc.length, zz_of)
            done.append(m)
    car_lines = [(ln, w) for ln, w, k, at in car if road_class(k, at["sub"]) != "parking"]
    for nx_, ny_, kind in _G["xnodes"].query(bb):
        if kind != "crossing" or not bb.contains(Point(nx_, ny_)):
            continue
        if any(np.hypot(nx_ - m[0], ny_ - m[1]) < 6 for m in done):
            continue
        best = None
        for ln, w in car_lines:
            d = ln.distance(Point(nx_, ny_))
            if d < 1.5 and (best is None or d < best[0]):
                best = (d, ln, w)
        if not best:
            continue
        _, ln, w = best
        t = ln.project(Point(nx_, ny_))
        p0, p1 = ln.interpolate(max(t - 1, 0)), ln.interpolate(min(t + 1, ln.length))
        tx, ty = p1.x - p0.x, p1.y - p0.y
        L = math.hypot(tx, ty) or 1
        zebra(nx_, ny_, -ty / L, tx / L, w, zz_of)
        done.append((nx_, ny_))

    # --- polní cesty: dvě vyjeté koleje
    for ln, w, kind, at in tracks:
        for part in shapely.get_parts(ln.intersection(bb)):
            if part.geom_type != "LineString" or part.length < 0.5:
                continue
            pts = np.asarray(shapely.segmentize(part, 2.0).coords)
            z = tz(pts[:, 0], pts[:, 1]) + 0.21
            left, right = offsets(pts, 0.75)
            strip(rb, left, z, 0.35, ROAD_STYLE["track"][0])
            strip(rb, right, z, 0.35, ROAD_STYLE["track"][0])

    # --- mosty: mostovka, boční hrany, zábradlí
    for ln, w, kind, at in bridges:
        cls = road_class(kind, at["sub"])
        col = ASPHALT if cls in CAR else FOOT_COL.get(cls, PAVING)
        for part in shapely.get_parts(ln.intersection(bb)):
            if part.geom_type != "LineString" or part.length < 0.5:
                continue
            pts = np.asarray(shapely.segmentize(part, 2.0).coords)
            z = line_z(ln, pts, True, tz, LIFT["car"])
            strip(rb, pts, z, w / 2, col)
            L, R = offsets(pts, w / 2 + 0.3)
            strip(rb, L, z + 0.1, 0.3, CURB)
            strip(rb, R, z + 0.1, 0.3, CURB)
            Lo, Ro = offsets(pts, w / 2 + 0.6)
            vwalls(mb, Ro[:-1], Ro[1:], z[:-1] - 0.9, z[1:] - 0.9, z[:-1] + 0.1, z[1:] + 0.1, W, (150, 148, 142))
            vwalls(mb, Lo[1:], Lo[:-1], z[1:] - 0.9, z[:-1] - 0.9, z[1:] + 0.1, z[:-1] + 0.1, W, (150, 148, 142))
            Li, Ri = offsets(pts, w / 2 + 0.5)
            vwalls(mb, Ri[:-1], Ri[1:], z[:-1] + 0.1, z[1:] + 0.1, z[:-1] + 1.1, z[1:] + 1.1, W, (90, 92, 96), both=True)
            vwalls(mb, Li[:-1], Li[1:], z[:-1] + 0.1, z[1:] + 0.1, z[:-1] + 1.1, z[1:] + 1.1, W, (90, 92, 96), both=True)
            for e in (0, -1):
                disc(rb, *pts[e], z[e], w / 2, col)

    # --- železnice
    for ln, bridge in _G["rails"].query(bb):
        for part in shapely.get_parts(ln.intersection(bb)):
            if part.geom_type != "LineString" or part.length < 0.5:
                continue
            pts = np.asarray(shapely.segmentize(part, 2.0).coords)
            z = line_z(ln, pts, bridge, tz, 0.30)
            strip(rb, pts, z, 1.6, (104, 96, 88))
            dashes(rb, pts, z, 2.4, 0.25, 0.4, (84, 70, 58), lift=0.03)
            lft, rgt = offsets(pts, 0.72)
            strip(rb, lft, z + 0.12, 0.05, (176, 176, 180))
            strip(rb, rgt, z + 0.12, 0.05, (176, 176, 180))

    # --- zdi, ploty, živé ploty
    BAR = {"wall": (0.15, 1.8, (176, 168, 156)), "retaining_wall": (0.25, 1.2, (160, 156, 148)),
           "fence": (0.03, 1.3, (96, 98, 96)), "hedge": (0.45, 1.5, (70, 104, 60))}
    for ln, kind in _G["barriers"].query(bb):
        hw, h, col = BAR[kind]
        thin_box_along(mb, ln.intersection(bb), hw, h, 0.0, tz, W, col)

    n = mb.count()
    if n:
        (OUT / "roads" / f"{i}_{j}.bin").write_bytes(mb.bytes())

    # --- mobiliář: lampy (OSM + podél ulic), zastávky, semafory
    props = []
    for x, y, kind in _G["xnodes"].query(bb):
        if not bb.contains(Point(x, y)):
            continue
        t = {"street_lamp": 0, "bus_stop": 1, "traffic_signals": 2}.get(kind)
        if t is None:
            continue
        rot = 0.0
        best = min(((ln.distance(Point(x, y)), ln) for ln, w in car_lines), default=None, key=lambda q: q[0])
        if best and best[0] < 25:
            pr = best[1].interpolate(best[1].project(Point(x, y)))
            rot = math.atan2(pr.x - x, -(pr.y - y))  # natočení k silnici (světové souřadnice)
            if t == 1:  # přístřešek dát na okraj vozovky
                pass
        props.append((x, y, t, rot))
    shapely.prepare(bld)
    for ln, w, kind, at in car:
        if not at.get("sidewalk"):
            continue
        part = ln.intersection(bb)
        for pc in shapely.get_parts(part):
            if pc.geom_type != "LineString" or pc.length < 10:
                continue
            side = 1 if (h32(int(pc.coords[0][0] * 10)) & 1) else -1
            for t in np.arange(12.0, pc.length, 30.0):
                p0 = pc.interpolate(t)
                p1 = pc.interpolate(min(t + 1, pc.length))
                tx, ty = p1.x - p0.x, p1.y - p0.y
                L = math.hypot(tx, ty) or 1
                nx_, ny_ = -ty / L * side, tx / L * side
                x, y = p0.x + nx_ * (w / 2 + 0.6), p0.y + ny_ * (w / 2 + 0.6)
                if bld.contains(Point(x, y)) or carriage.contains(Point(x, y)):
                    continue
                props.append((x, y, 0, math.atan2(-nx_, ny_)))
                if w >= 9:
                    side = -side
    if props:
        P = np.asarray(props, np.float64)
        wx, wz = W(P[:, 0], P[:, 1])
        y = tz(P[:, 0], P[:, 1]) + LIFT["sidewalk"]
        arr = np.column_stack([wx, y, wz, P[:, 2], P[:, 3]]).astype(np.float32)
        (OUT / "props" / f"{i}_{j}.bin").write_bytes(arr.tobytes())
    return ij, n, len(props)


# --------------------------------------------------------------------------- jízda autem
def export_drive(per_chunk, roads, tz, W):
    """drive_fp.bin: půdorysy budov pro kolize; drive.json: názvy ulic + mosty s výškou."""
    rings = []
    for items in per_chunk.values():
        for _, poly, _ in items:
            ring = np.asarray(poly.simplify(0.4).exterior.coords)[:-1]
            if len(ring) >= 3:
                wx, wz = W(ring[:, 0], ring[:, 1])
                rings.append(np.column_stack([wx, wz]).astype(np.float32))
    offs = np.concatenate([[0], np.cumsum([len(r) for r in rings])]).astype(np.uint32)
    head = np.array([len(rings)], np.uint32)
    (OUT / "drive_fp.bin").write_bytes(head.tobytes() + offs.tobytes() + np.concatenate(rings).tobytes())

    streets, bridges = [], []
    for ln, w, kind, at in roads:
        if kind in ("footway", "path", "steps", "cycleway", "bridleway"):
            continue
        pts = np.asarray(shapely.segmentize(ln, 4.0).coords)
        wx, wz = W(pts[:, 0], pts[:, 1])
        if at.get("name"):
            simp = np.asarray(ln.simplify(2.0).coords)
            sx, sz = W(simp[:, 0], simp[:, 1])
            streets.append([at["name"], np.round(np.column_stack([sx, sz]).ravel(), 1).tolist()])
        if at["bridge"]:
            z = line_z(ln, pts, True, tz, ROAD_STYLE["road"][2])
            bridges.append([round(w / 2 + 0.5, 1), np.round(np.column_stack([wx, z, wz]).ravel(), 2).tolist()])
    (OUT / "drive.json").write_text(json.dumps({"streets": streets, "bridges": bridges},
                                               ensure_ascii=False, separators=(",", ":")))
    log(f"  půdorysů {len(rings)}, ulic {len(streets)}, mostů {len(bridges)}")


# --------------------------------------------------------------------------- stromy
TREE_DECID, TREE_CONIF = 0, 1


def detect_trees(ext, veg, bmask, needle_mask_fn):
    gv = Grid(ext, VEG_RES)
    a = veg.copy()
    hole = np.isnan(a)
    # díry (řídká data) → max okolí
    a = np.where(hole, ndimage.maximum_filter(np.nan_to_num(a), size=3), a)
    a[bmask] = 0
    s = ndimage.gaussian_filter(a, 0.8)
    mx = ndimage.maximum_filter(s, size=3)
    peaks = (s == mx) & (s > 3.0) & (a > 2.5)
    rr, cc = np.nonzero(peaks)
    h = np.maximum(a[rr, cc], s[rr, cc])
    mean5 = ndimage.uniform_filter(s, 5)[rr, cc]
    sharp = (s[rr, cc] - mean5) / np.maximum(s[rr, cc], 1)
    rng = np.random.default_rng(1)
    x = gv.x0 + (cc + rng.uniform(0.2, 0.8, len(cc))) * gv.res
    y = gv.y1 - (rr + rng.uniform(0.2, 0.8, len(rr))) * gv.res
    t = np.column_stack([x, y, h, sharp])
    conif = (t[:, 3] > 0.18) | needle_mask_fn(t[:, 0], t[:, 1])
    return t, conif


# --------------------------------------------------------------------------- main
def main():
    ext = extent()
    log("extent", ext)
    dtm = build_dtm(ext)
    dtm_at = dtm_sampler(ext, dtm)
    bpts, veg = build_surface(ext, dtm_at)
    btree = cKDTree(bpts[:, :2])
    gn = Grid(ext, VEG_RES)
    z0 = math.floor(float(dtm.min()))
    ox, oy = (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2

    def W(x, y):
        return (x - ox, oy - y)

    nci = int((ext[2] - ext[0]) / CHUNK)
    ncj = int((ext[3] - ext[1]) / CHUNK)
    chunks = [(i, j) for i in range(nci) for j in range(ncj)]

    def chunk_of(x, y):
        return (int((x - ext[0]) // CHUNK), int((ext[3] - y) // CHUNK))

    # ---- terén
    log("terén")
    (OUT / "terrain").mkdir(exist_ok=True)
    for i, j in chunks:
        xs = ext[0] + i * CHUNK + np.arange(TERRAIN_N + 1) * TERRAIN_RES
        ys = ext[3] - j * CHUNK - np.arange(TERRAIN_N + 1) * TERRAIN_RES
        gx, gy = np.meshgrid(xs, ys)
        z = dtm_at(gx.ravel(), gy.ravel())
        q = np.clip((z - z0) * 20, 0, 65535).astype(np.uint16)
        (OUT / "terrain" / f"{i}_{j}.bin").write_bytes(q.tobytes())

    # ---- OSM
    log("OSM")
    els = load_osm()
    buildings, land, water, waterways, roads, rails, labels, needle = [], [], [], [], [], [], [], []
    for e in els:
        tg = e.get("tags", {})
        if "building" in tg and e["type"] in ("way", "relation"):
            p = element_polys(e)
            if p is not None and not p.is_empty:
                buildings.append((e["id"], p, tg))
            continue
        if "highway" in tg and e["type"] == "way":
            kind = tg["highway"]
            if kind in ROAD_W and tg.get("area") != "yes" and tg.get("tunnel") not in ("yes",):
                ln = element_line(e)
                if ln is not None:
                    w = ROAD_W[kind]
                    try:
                        lanes = float(tg.get("lanes", 0))
                    except ValueError:
                        lanes = 0
                    if lanes and kind not in ("footway", "path", "cycleway", "steps", "track"):
                        w = max(w, lanes * 3.1 + 0.6)
                    try:
                        w = float(str(tg.get("width", w)).replace(",", ".").rstrip(" m"))
                    except ValueError:
                        pass
                    sub = tg.get("footway") or tg.get("service") or ""
                    if tg.get("crossing") or sub == "crossing":
                        sub = "crossing"
                    roads.append((ln, w, kind, dict(
                        bridge=tg.get("bridge") not in (None, "no"), sub=sub, lanes=lanes,
                        oneway=tg.get("oneway") == "yes", surface=tg.get("surface", ""),
                        sidewalk_tag=tg.get("sidewalk") or tg.get("sidewalk:both"),
                        name=tg.get("name") or tg.get("ref") or "")))
            continue
        if "railway" in tg and e["type"] == "way":
            ln = element_line(e)
            if ln is not None and tg.get("tunnel") != "yes" and tg.get("railway") in ("rail", "tram", "light_rail"):
                rails.append((ln, tg.get("bridge") not in (None, "no")))
            continue
        if "waterway" in tg and e["type"] == "way":
            ln = element_line(e)
            if ln is not None and tg.get("tunnel") not in ("yes", "culvert"):
                w = {"river": 8, "canal": 6, "stream": 3, "ditch": 1.5, "drain": 1.5}.get(tg["waterway"], 0)
                if w:
                    waterways.append((ln, w))
            continue
        if tg.get("natural") == "water" or "water" in tg or tg.get("landuse") in ("reservoir", "basin"):
            p = element_polys(e)
            if p is not None:
                water.append(p)
            continue
        col = (LANDUSE_COL.get(tg.get("landuse")) or NATURAL_COL.get(tg.get("natural"))
               or LEISURE_COL.get(tg.get("leisure")))
        if tg.get("amenity") == "parking":
            col = (156, 156, 152)
        if col:
            p = element_polys(e)
            if p is not None and not p.is_empty:
                land.append((p, col))
                if tg.get("leaf_type") == "needleleaved":
                    needle.append(p)
            continue

    for e in els:
        tg = e.get("tags", {})
        name = tg.get("name")
        if not name:
            continue
        kind = None
        if "place" in tg:
            kind = tg["place"]
        elif (tg.get("tourism") in ("attraction", "museum", "zoo", "viewpoint")
              or tg.get("historic") in ("castle", "church", "monument", "city_gate", "tower", "manor",
                                        "monastery", "chapel", "building", "city_walls")
              or tg.get("amenity") in ("townhall", "theatre")
              or (tg.get("amenity") == "place_of_worship" and e["type"] != "node")):
            kind = "poi"
        if not kind or ("building" in tg and kind != "poi"):
            continue
        if e["type"] == "node":
            x, y = T.transform(e["lon"], e["lat"])
        elif "center" in e:
            x, y = T.transform(e["center"]["lon"], e["center"]["lat"])
        elif "bounds" in e:
            b = e["bounds"]
            x, y = T.transform((b["minlon"] + b["maxlon"]) / 2, (b["minlat"] + b["maxlat"]) / 2)
        else:
            continue
        if not (ext[0] < x < ext[2] and ext[1] < y < ext[3]):
            continue
        wx, wz = W(x, y)
        labels.append(dict(name=name, kind=kind, x=round(wx, 1), z=round(wz, 1),
                           y=round(float(dtm_at(x, y)[0]) - z0, 1)))
    # landuse: větší plochy první, menší přes ně
    land.sort(key=lambda t: -t[0].area)
    log(f"budov {len(buildings)}, silnic {len(roads)}, land {len(land)}, voda {len(water)}, popisků {len(labels)}")

    # chodníky: tag sidewalk, jinak ulice v zástavbě (do 30 m od budovy) → obě strany
    btree_geo = shapely.STRtree([g for _, g, _ in buildings])
    for ln, w, kind, at in roads:
        tg_sw = at.pop("sidewalk_tag", None)
        if tg_sw in ("no", "none", "separate"):
            continue
        if tg_sw in ("both", "left", "right"):
            at["sidewalk"] = tg_sw
        elif kind in ("primary", "secondary", "tertiary", "residential", "unclassified", "living_street") \
                and not at["bridge"] and len(btree_geo.query(ln.buffer(30), predicate="intersects")):
            at["sidewalk"] = "both"

    # ---- textury
    log("textury")
    render_textures(ext, land, water, waterways, roads, rails, chunks)

    # ---- budovy
    log("budovy")
    per_chunk = {c: [] for c in chunks}
    bimg = Image.new("1", (gn.nx, gn.ny), 0)
    bdraw = ImageDraw.Draw(bimg)
    for oid, geom, tg in buildings:
        for poly in shapely.get_parts(geom):
            if poly.geom_type != "Polygon" or poly.area < 6:
                continue
            poly = poly.simplify(0.05)
            bdraw.polygon([((x - gn.x0) / gn.res, (gn.y1 - y) / gn.res)
                           for x, y in poly.buffer(0.5).exterior.coords], fill=1)
            ch = chunk_of(poly.centroid.x, poly.centroid.y)
            if ch in per_chunk:
                per_chunk[ch].append((oid, poly, tg))
    (OUT / "buildings").mkdir(exist_ok=True)
    allpolys = [it[1] for its in per_chunk.values() for it in its]
    _G.update(bpts=bpts, btree=btree, dtm_at=dtm_at, z0=z0, W=W,
              allpolys=allpolys, ptree=shapely.STRtree(allpolys))
    jobs = sorted(((c, it) for c, it in per_chunk.items() if it), key=lambda t: -len(t[1]))
    with _pool() as pool:
        bcount = {c: n for c, n in pool.imap_unordered(_bld_chunk, jobs) if n}
    log(f"  budovy hotovo, dlaždic {len(bcount)}")

    # ---- 3D silnice
    log("silnice 3D")
    (OUT / "roads").mkdir(exist_ok=True)
    (OUT / "props").mkdir(exist_ok=True)
    _G["tz"] = terrain_mesh_sampler(ext, dtm_at, z0)
    areas, parking, barriers, xnodes = [], [], [], []
    extra = RAW / "osm_extra.json"
    for e in (json.loads(extra.read_text())["elements"] if extra.exists() else []):
        tg = e.get("tags", {})
        if e["type"] == "node":
            k = tg.get("highway")
            if k in ("crossing", "street_lamp", "bus_stop", "traffic_signals"):
                x, y = T.transform(e["lon"], e["lat"])
                xnodes.append((x, y, k))
            continue
        if "barrier" in tg:
            ln = element_line(e)
            if ln is not None and tg["barrier"] in ("wall", "retaining_wall", "fence", "hedge"):
                barriers.append((ln, tg["barrier"]))
            continue
        poly = element_polys(e)
        if poly is None or poly.is_empty:
            continue
        if tg.get("amenity") == "parking" and tg.get("parking", "surface") == "surface":
            parking.append(poly)
        elif "area:highway" in tg:
            areas.append((poly, "ped" if tg["area:highway"] in ("footway", "pedestrian", "cycleway", "path") else "car"))
        elif tg.get("highway") or tg.get("place") == "square":
            areas.append((poly, "car" if tg.get("highway") in ("service", "residential", "unclassified") else "ped"))
    log(f"  plochy {len(areas)}, parkoviště {len(parking)}, bariéry {len(barriers)}, uzly {len(xnodes)}")
    _G.update(
        areas=Layer(areas, lambda t: t[0]),
        parking=Layer(parking, lambda t: t),
        barriers=Layer(barriers, lambda t: t[0]),
        xnodes=Layer(xnodes, lambda t: Point(t[0], t[1])),
        allpolys_layer=Layer(allpolys, lambda t: t),
    )
    with _pool() as pool:
        res = list(pool.imap_unordered(_road_chunk, chunks))
    rcount = {c: n for c, n, _ in res if n}
    pcount = {c: k for c, _, k in res if k}
    log(f"  silnic: dlaždic {len(rcount)}, mobiliář {sum(pcount.values())}")

    # ---- data pro jízdu autem
    log("data pro jízdu")
    export_drive(per_chunk, roads, _G["tz"], W)

    # ---- stromy
    log("stromy")
    bmask = np.asarray(bimg, dtype=bool)
    needle_u = unary_union(needle) if needle else None

    def needle_fn(x, y):
        if needle_u is None:
            return np.zeros(len(x), bool)
        return shapely.contains_xy(needle_u, x, y)

    trees, conif = detect_trees(ext, veg, bmask, needle_fn)
    log(f"  stromů {len(trees)}")
    (OUT / "trees").mkdir(exist_ok=True)
    tz = dtm_at(trees[:, 0], trees[:, 1]) - z0
    wx, wz = trees[:, 0] - ox, oy - trees[:, 1]
    h = trees[:, 2]
    r = np.clip(0.2 * h + 0.8, 1.2, 6.5)
    arr = np.column_stack([wx, tz, wz, h, r, conif.astype(np.float32)]).astype(np.float32)
    ci = ((trees[:, 0] - ext[0]) // CHUNK).astype(int)
    cj = ((ext[3] - trees[:, 1]) // CHUNK).astype(int)
    tcount = {}
    for i, j in chunks:
        m = (ci == i) & (cj == j)
        if m.any():
            (OUT / "trees" / f"{i}_{j}.bin").write_bytes(arr[m].tobytes())
            tcount[(i, j)] = int(m.sum())

    (OUT / "labels.json").write_text(json.dumps(labels, ensure_ascii=False))
    sx, sy = T.transform(*START)
    swx, swz = W(sx, sy)
    meta = dict(
        city=CITY, name=CITIES[CITY]["name"], version=int(time.time()),
        start=[round(swx, 1), round(float(dtm_at(sx, sy)[0]) - z0, 1), round(swz, 1)],
        extent=ext, origin=[ox, oy], z0=z0, chunk=CHUNK, terrainN=TERRAIN_N, nx=nci, ny=ncj,
        chunks=[dict(i=i, j=j, b=bcount.get((i, j), 0), t=tcount.get((i, j), 0), r=rcount.get((i, j), 0),
                     p=pcount.get((i, j), 0))
                for i, j in chunks],
    )
    (OUT / "meta.json").write_text(json.dumps(meta))
    write_regions()
    log("hotovo")


if __name__ == "__main__":
    main()
