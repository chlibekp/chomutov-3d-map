"""Konfigurace pipeline. Město se volí proměnnou prostředí CITY (výchozí: chomutov).

    CITY=velke-mezirici ../.venv/bin/python download.py
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CITIES = {
    "chomutov": dict(
        name="Chomutov · Jirkov",
        bbox=dict(south=50.43, west=13.32, north=50.53, east=13.50),
        start=(13.4176, 50.4606),  # nám. 1. Máje
    ),
    "velke-mezirici": dict(
        name="Velké Meziříčí",
        bbox=dict(south=49.325, west=15.95, north=49.39, east=16.08),
        start=(16.0125, 49.3553),  # Radniční náměstí
    ),
}

CITY = os.environ.get("CITY", "chomutov")
if CITY not in CITIES:
    raise SystemExit(f"Neznámé město '{CITY}'. Dostupná: {', '.join(CITIES)}")
BBOX = CITIES[CITY]["bbox"]
START = CITIES[CITY]["start"]

SHARED = ROOT / "data" / "shared"           # celostátní indexy (ATOM feedy, RÚIAN)
RAW = ROOT / "data" / "raw" / CITY
WORK = ROOT / "data" / "work" / CITY
OUT_ROOT = ROOT / "web" / "public" / "data"
OUT = OUT_ROOT / CITY

CRS = "EPSG:5514"  # S-JTSK Krovak East North (x=east, y=north, metry)

TERRAIN_RES = 4.0  # m, výstupní heightmapa terénu
NDSM_RES = 1.0     # m, pracovní rastr pro výšky budov/stromů
CHUNK = 1000.0     # m, velikost dlaždice terénu

for p in (SHARED, RAW, WORK, OUT):
    p.mkdir(parents=True, exist_ok=True)


def write_regions():
    """Seznam měst s hotovými daty pro výběr ve webu."""
    regions = [dict(id=k, name=v["name"]) for k, v in CITIES.items() if (OUT_ROOT / k / "meta.json").exists()]
    (OUT_ROOT / "regions.json").write_text(json.dumps(regions, ensure_ascii=False))
