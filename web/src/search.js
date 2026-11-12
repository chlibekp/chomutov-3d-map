import { DATA } from './region.js';

// Vyhledávání: adresy (RÚIAN), ulice, místa a POI (OSM). Bez diakritiky, prefixové shody slov.

const KIND_W = { 'místo': 0, 'ulice': 1, 'místo zájmu': 2, 'budova': 2, 'oblast': 3, 'voda': 3, 'adresa': 4 };
const ICON = { 'místo': '◉', 'ulice': '━', 'místo zájmu': '★', 'budova': '▣', 'oblast': '▢', 'voda': '≈', 'adresa': '⌂' };

export const norm = (s) => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const words = (s) => norm(s).split(/[\s,·.()/-]+/).filter(Boolean);

let items = null;
async function load() {
  if (items) return items;
  const d = await (await fetch(`${DATA}/search.json`)).json();
  items = d.items.map(([label, sub, kind, x, y, z, extra]) => {
    const lw = words(label);
    const all = new Set([...lw, ...words(sub), ...words(extra || ''), ...norm(label).split(/\s+/)]);
    return { label, sub, kind, x, y, z, nl: norm(label), lw, all: [...all] };
  });
  return items;
}

function search(q, limit = 8) {
  const nq = norm(q).trim();
  const qt = words(q);
  if (!qt.length) return [];
  const out = [];
  for (const it of items) {
    let ok = true, exact = 0, inLabel = 0;
    for (const t of qt) {
      let hit = false;
      for (const w of it.all) {
        if (w.startsWith(t)) { hit = true; if (w === t) exact++; break; }
      }
      if (!hit) { ok = false; break; }
      if (it.lw.some((w) => w.startsWith(t))) inLabel++;
    }
    if (!ok) continue;
    const score = (it.nl === nq ? -100 : 0) + (it.nl.startsWith(nq) ? -20 : 0) - inLabel * 6 - exact * 3
      + KIND_W[it.kind] * 4 + it.label.length * 0.05;
    out.push({ it, score });
  }
  out.sort((a, b) => a.score - b.score);
  return out.slice(0, limit).map((o) => o.it);
}

export function initSearch(onSelect) {
  const input = document.getElementById('search');
  const list = document.getElementById('results');
  let res = [], sel = 0;

  const render = () => {
    list.innerHTML = '';
    list.hidden = !res.length;
    res.forEach((it, k) => {
      const li = document.createElement('li');
      li.className = k === sel ? 'sel' : '';
      li.innerHTML = `<span class="ic">${ICON[it.kind] || '•'}</span><span class="t"><b></b><small></small></span>`;
      li.querySelector('b').textContent = it.label;
      li.querySelector('small').textContent = `${it.kind} · ${it.sub}`;
      li.onmousedown = (e) => { e.preventDefault(); choose(k); };
      list.appendChild(li);
    });
  };
  const choose = (k) => {
    const it = res[k];
    if (!it) return;
    input.value = it.label;
    res = []; render();
    input.blur();
    onSelect(it);
  };

  input.addEventListener('focus', load, { once: true });
  input.addEventListener('input', async () => {
    await load();
    res = search(input.value); sel = 0; render();
  });
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'ArrowDown') { sel = Math.min(res.length - 1, sel + 1); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
    else if (e.key === 'Enter') choose(sel);
    else if (e.key === 'Escape') { res = []; render(); input.blur(); }
  });
  input.addEventListener('blur', () => setTimeout(() => { list.hidden = true; }, 100));
  addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== input) { e.preventDefault(); input.focus(); input.select(); }
  });
}
