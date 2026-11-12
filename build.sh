#!/usr/bin/env bash
# Vyrobí všechna data pro mapu (lidar ČÚZK, OSM, RÚIAN → web/public/data) a připraví web.
#
#   ./build.sh                      # všechna města z pipeline/config.py
#   ./build.sh velke-mezirici       # jen vybraná města
#   ./build.sh --serve              # po sestavení spustí vývojový server
#
# Kroky jsou idempotentní: stažené soubory se znovu nestahují (smaž data/raw/<město> pro obnovu).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

SERVE=0
CITIES=()
for a in "$@"; do
  case "$a" in
    --serve) SERVE=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) CITIES+=("$a") ;;
  esac
done

log() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- Python
PY=python3
command -v "$PY" >/dev/null || { echo "Chybí python3" >&2; exit 1; }
if [ ! -x .venv/bin/python ]; then
  log "Vytvářím .venv"
  "$PY" -m venv .venv
fi
log "Instaluji Python balíčky"
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -r pipeline/requirements.txt

if [ ${#CITIES[@]} -eq 0 ]; then
  # shellcheck disable=SC2207
  CITIES=($(cd pipeline && ../.venv/bin/python -c "import config; print(' '.join(config.CITIES))"))
fi

# ---------------------------------------------------------------- RÚIAN adresy (celá ČR, sdílené)
RUIAN="data/shared/ruian/adr.zip"
if [ ! -s "$RUIAN" ]; then
  log "Stahuji adresní místa RÚIAN"
  mkdir -p "$(dirname "$RUIAN")"
  # export je k poslednímu dni měsíce; zkusí minulý a předminulý měsíc
  for back in 1 2; do
    D=$(.venv/bin/python - "$back" <<'EOF'
import sys, datetime
d = datetime.date.today().replace(day=1)
for _ in range(int(sys.argv[1])):
    d = (d - datetime.timedelta(days=1)).replace(day=1)
nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
print((nxt - datetime.timedelta(days=1)).strftime("%Y%m%d"))
EOF
)
    URL="https://vdp.cuzk.gov.cz/vymenny_format/csv/${D}_OB_ADR_csv.zip"
    if curl -fsSL -o "$RUIAN.part" "$URL"; then mv "$RUIAN.part" "$RUIAN"; break; fi
    echo "  $URL nedostupné, zkouším starší"
  done
  [ -s "$RUIAN" ] || { echo "Nepodařilo se stáhnout RÚIAN" >&2; exit 1; }
fi

# ---------------------------------------------------------------- města
for c in "${CITIES[@]}"; do
  log "[$c] stahování (lidar ČÚZK + OSM)"
  (cd pipeline && CITY="$c" ../.venv/bin/python download.py)
  log "[$c] zpracování"
  (cd pipeline && CITY="$c" ../.venv/bin/python process.py)
  log "[$c] vyhledávací index"
  (cd pipeline && CITY="$c" ../.venv/bin/python search.py)
done

# ---------------------------------------------------------------- web
command -v npm >/dev/null || { echo "Chybí npm (Node.js)" >&2; exit 1; }
log "Instaluji webové závislosti"
(cd web && npm install --silent)

log "Hotovo: ${CITIES[*]}"
if [ "$SERVE" -eq 1 ]; then
  cd web && exec npx vite
else
  echo "Spusť:  cd web && npm run dev"
fi
