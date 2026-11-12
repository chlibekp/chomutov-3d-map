// Zvolené město: ?mesto=<id> v URL (výchozí Chomutov). Data leží v /data/<id>/.
export const REGION = new URLSearchParams(location.search).get('mesto') || 'chomutov';
export const DATA = `/data/${REGION}`;

export async function initRegionSelect() {
  const sel = document.getElementById('city');
  let regions = [];
  try {
    regions = await (await fetch('/data/regions.json')).json();
  } catch { /* bez seznamu jen aktuální město */ }
  if (!regions.some((r) => r.id === REGION)) regions.unshift({ id: REGION, name: REGION });
  for (const r of regions) {
    const o = document.createElement('option');
    o.value = r.id;
    o.textContent = r.name;
    o.selected = r.id === REGION;
    sel.appendChild(o);
  }
  sel.onchange = () => {
    const u = new URL(location.href);
    u.searchParams.set('mesto', sel.value);
    location.href = u.toString();
  };
  return regions.find((r) => r.id === REGION);
}
