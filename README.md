# Chomutov · Jirkov 3D

Stylizovaná interaktivní 3D mapa Chomutova, Jirkova a okolí.

**Data:** ČÚZK DMR 5G (terén), DMP 1G (lidar: výšky budov, stromy), RÚIAN (adresní místa pro vyhledávání), OpenStreetMap (půdorysy, silnice, voda, land use, popisky).

## Spuštění

Vše jedním skriptem (vyžaduje Python 3.11+ a Node.js 18+):

```bash
./build.sh                    # všechna města: stáhne a zpracuje data, nainstaluje web
./build.sh velke-mezirici     # jen vybrané město
./build.sh --serve            # po sestavení spustí server (http://localhost:5173)
```

### Ručně

Města jsou definovaná v `pipeline/config.py` (`CITIES`: název, bbox, výchozí bod). Město se volí proměnnou `CITY`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
mkdir -p data/shared/ruian && curl -o data/shared/ruian/adr.zip \
  https://vdp.cuzk.gov.cz/vymenny_format/csv/20261031_OB_ADR_csv.zip   # adresy RÚIAN (celá ČR, poslední den měsíce)

cd pipeline
for c in chomutov velke-mezirici; do
  CITY=$c ../.venv/bin/python download.py   # lidar ČÚZK + OSM → data/raw/<město>
  CITY=$c ../.venv/bin/python process.py    # → web/public/data/<město>
  CITY=$c ../.venv/bin/python search.py     # → web/public/data/<město>/search.json
done
cd ../web && npm install && npm run dev      # město: ?mesto=<id>, nebo výběr v panelu
```

Nové město přidáš záznamem do `CITIES` a spuštěním tří kroků výše. Ve výběru se objeví automaticky
(`web/public/data/regions.json`).

## Nasazení (Cloudflare Pages)

Data se generují lokálně (lidar se v CI stahovat nedá), takže se na Pages nahrává hotový `web/dist`
přímo přes Wrangler, ne přes Git integraci.

```bash
./build.sh                         # vygeneruje data do web/public/data
cd web
npx wrangler login                 # jednou
npm run deploy                     # vite build + wrangler pages deploy (projekt chomutov-3d-map)
```

První `deploy` nabídne vytvoření projektu. Nastavení je ve `web/wrangler.toml`, cache hlavičky
ve `web/public/_headers`. Limity Pages: max. 20 000 souborů a 25 MiB na soubor (nyní ~2 000 souborů, největší ~13 MB).

## Jak to funguje

- **Terén:** body DMR 5G → TIN interpolace na 2m mřížku → dlaždice 1×1 km (4 m/vrchol), 2 úrovně LOD.
- **Budovy:** půdorys z OSM. Výška se bere z lidarových bodů střech (třída 6), z jejich lineární regrese
  výšky vůči vzdálenosti od hřebene (osa OBB). Podle sklonu se rozhodne sedlová/plochá střecha.
  Okna generuje procedurálně shader a v noci náhodně svítí.
- **Stromy:** lokální maxima rastru vegetace (třída 5) → poloha a výška každého stromu.
  Jehličnan/listnáč se určí podle ostrosti koruny a OSM `leaf_type`. Vykresluje se přes InstancedMesh.
- **Podklad:** land use, silnice, voda a železnice se rasterizují do textury 1 m/px.
- **Vyhledávání:** všech 13 419 adresních míst RÚIAN v rozsahu mapy, ulice, místa a POI z OSM.
  Hledá se bez diakritiky a podle prefixů slov („palackeho 40“, „jirkov 123“, „čp 1“, „43001“).

## Ovládání

WASD/šipky posun · Q/E otočení · R/F náklon · Shift rychleji · dvojklik přelet · `/` hledat
