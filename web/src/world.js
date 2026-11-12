import * as THREE from 'three';

import { DATA } from './region.js';

// ---------------------------------------------------------------- materiály
export const uniforms = {
  uNight: { value: 0 },
  uLitFrac: { value: 0.4 },
};

export function buildingMaterial() {
  const m = new THREE.MeshLambertMaterial({ vertexColors: true });
  m.onBeforeCompile = (sh) => {
    sh.uniforms.uNight = uniforms.uNight;
    sh.uniforms.uLitFrac = uniforms.uLitFrac;
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec2 vWin;')
      .replace('#include <uv_vertex>', '#include <uv_vertex>\nvWin = uv;');
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying vec2 vWin;\nuniform float uNight;\nuniform float uLitFrac;')
      .replace('#include <color_fragment>', `#include <color_fragment>
        float winLit = 0.0;
        if (vWin.x >= 0.0) {
          vec2 cell = vec2(vWin.x / 2.7, (vWin.y - 0.8) / 3.0);
          vec2 f = fract(cell);
          vec2 id = floor(cell);
          float win = step(0.32, f.x) * step(f.x, 0.72) * step(0.25, f.y) * step(f.y, 0.75) * step(0.0, cell.y);
          float h = fract(sin(dot(id, vec2(12.9898, 78.233))) * 43758.5453);
          diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.13, 0.16, 0.2), win * 0.8);
          winLit = win * step(1.0 - uLitFrac, h);
        }`)
      .replace('#include <emissivemap_fragment>', `#include <emissivemap_fragment>
        totalEmissiveRadiance += vec3(1.0, 0.72, 0.38) * winLit * uNight * 0.9;`);
  };
  return m;
}

const texLoader = new THREE.TextureLoader();
let maxAniso = 4;
export function setAniso(a) { maxAniso = a; }

// Chyba „přerušeno“ – po ní se nic neuklízí ani nehlásí.
const aborted = () => new DOMException('Aborted', 'AbortError');

async function fetchBuf(url, signal) {
  const r = await fetch(url, { signal });
  if (!r.ok) throw Object.assign(new Error(`${r.status} ${url}`), { missing: r.status === 404 });
  return r.arrayBuffer();
}

// Textura přes fetch (kvůli AbortSignal), dekódování přes TextureLoader z blob URL.
async function loadTex(url, signal) {
  const r = await fetch(url, { signal });
  if (!r.ok) throw Object.assign(new Error(`${r.status} ${url}`), { missing: r.status === 404 });
  const src = URL.createObjectURL(await r.blob());
  try {
    return await new Promise((res, rej) => texLoader.load(src, (t) => {
      t.colorSpace = THREE.SRGBColorSpace;
      t.anisotropy = maxAniso;
      t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
      res(t);
    }, undefined, rej));
  } finally { URL.revokeObjectURL(src); }
}

// ---------------------------------------------------------------- workery
const POOL = Array.from({ length: Math.min(4, Math.max(2, (navigator.hardwareConcurrency || 4) - 2)) },
  () => new Worker(new URL('./worker.js', import.meta.url), { type: 'module' }));
let seq = 0, rr = 0;
const waiting = new Map();
for (const w of POOL) {
  w.onmessage = (e) => {
    const { id, result, error } = e.data;
    const p = waiting.get(id);
    waiting.delete(id);
    error ? p.rej(new Error(error)) : p.res(result);
  };
}
function work(type, args, transfer = []) {
  const id = ++seq;
  return new Promise((res, rej) => {
    waiting.set(id, { res, rej });
    POOL[rr++ % POOL.length].postMessage({ id, type, args }, transfer);
  });
}

// ---------------------------------------------------------------- stromy
const crownGeo = new THREE.IcosahedronGeometry(1, 0);
const coneGeo = (() => { const g = new THREE.ConeGeometry(1, 1, 7); g.translate(0, 0.5, 0); return g; })();
const trunkGeo = (() => { const g = new THREE.CylinderGeometry(0.5, 0.6, 1, 5); g.translate(0, 0.5, 0); return g; })();
const treeMat = new THREE.MeshLambertMaterial({ flatShading: true });
const trunkMat = new THREE.MeshLambertMaterial({ color: 0x4a3a2e });
const SHARED = new Set([crownGeo, coneGeo, trunkGeo]);

function instanced(geo, mat, matrices, colors) {
  const n = matrices.length / 16;
  if (!n) return null;
  const im = new THREE.InstancedMesh(geo, mat, n);
  im.instanceMatrix = new THREE.InstancedBufferAttribute(matrices, 16);
  if (colors) im.instanceColor = new THREE.InstancedBufferAttribute(colors, 3);
  im.castShadow = im.receiveShadow = true;
  im.computeBoundingSphere();
  return im;
}

function meshGeometry({ pos, uv, col, nrm }) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.BufferAttribute(nrm, 3));
  g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  g.setAttribute('color', new THREE.BufferAttribute(col, 4, true));
  g.computeBoundingSphere();
  return g;
}

// ---------------------------------------------------------------- mobiliář
const metal = new THREE.MeshLambertMaterial({ color: 0x3c4046 });
const lampHeadMat = new THREE.MeshLambertMaterial({ color: 0xfff1d0, emissive: 0xffc56a, emissiveIntensity: 0 });
const glassMat = new THREE.MeshLambertMaterial({ color: 0x9fb8c8, transparent: true, opacity: 0.35 });
const signalMat = new THREE.MeshLambertMaterial({ color: 0x222222 });
function poolTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  gr.addColorStop(0, 'rgba(255,200,120,1)');
  gr.addColorStop(0.45, 'rgba(255,170,80,0.35)');
  gr.addColorStop(1, 'rgba(255,150,60,0)');
  g.fillStyle = gr; g.fillRect(0, 0, 128, 128);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
const poolMat = new THREE.MeshBasicMaterial({
  map: poolTexture(), transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending, fog: false,
  polygonOffset: true, polygonOffsetFactor: -4,
});
const PROP_GEO = (() => {
  const box = (w, h, d, x, y, z) => { const g = new THREE.BoxGeometry(w, h, d); g.translate(x, y, z); return g; };
  const cyl = (r, h, x, y, z) => { const g = new THREE.CylinderGeometry(r * 0.8, r, h, 6); g.translate(x, y + h / 2, z); return g; };
  const pool = new THREE.PlaneGeometry(1, 1); pool.rotateX(-Math.PI / 2);
  return {
    pole: cyl(0.09, 6.5, 0, 0, 0),
    arm: box(0.08, 0.08, 1.4, 0, 6.45, 0.65),
    head: box(0.35, 0.12, 0.7, 0, 6.38, 1.3),
    pool,
    shelterFrame: [box(3.2, 0.1, 1.5, 0, 2.4, 0), cyl(0.05, 2.4, -1.5, 0, -0.6), cyl(0.05, 2.4, 1.5, 0, -0.6),
      cyl(0.05, 2.4, -1.5, 0, 0.6), cyl(0.05, 2.4, 1.5, 0, 0.6), box(1.8, 0.08, 0.35, 0, 0.45, -0.5)],
    shelterGlass: box(3.0, 2.2, 0.04, 0, 1.2, -0.68),
    signal: [cyl(0.07, 3.2, 0, 0, 0), box(0.3, 0.9, 0.25, 0, 3.4, 0.1)],
  };
})();
const PROP_SHARED = new Set(Object.values(PROP_GEO).flat());

export function updateProps(night) {
  lampHeadMat.emissiveIntensity = 0.2 + night * 4;
  poolMat.opacity = night * 0.55;
}

function propsGroup(arr) {
  const n = arr.length / 5;
  const by = [[], [], []];
  for (let k = 0; k < n; k++) by[arr[k * 5 + 3] | 0]?.push(k);
  const g = new THREE.Group();
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), p = new THREE.Vector3(), up = new THREE.Vector3(0, 1, 0);
  const one = new THREE.Vector3(1, 1, 1);
  const inst = (geo, mat, idx, scale = one, yoff = 0, shadow = true) => {
    if (!idx.length) return;
    const im = new THREE.InstancedMesh(geo, mat, idx.length);
    idx.forEach((k, t) => {
      p.set(arr[k * 5], arr[k * 5 + 1] + yoff, arr[k * 5 + 2]);
      q.setFromAxisAngle(up, arr[k * 5 + 4]);
      im.setMatrixAt(t, m.compose(p, q, scale));
    });
    im.castShadow = shadow; im.receiveShadow = true;
    im.computeBoundingSphere();
    g.add(im);
    return im;
  };
  const [lamps, stops, signals] = by;
  inst(PROP_GEO.pole, metal, lamps);
  inst(PROP_GEO.arm, metal, lamps);
  inst(PROP_GEO.head, lampHeadMat, lamps, one, 0, false);
  // světelná skvrna na zemi pod hlavou lampy
  const pool = inst(PROP_GEO.pool, poolMat, lamps, new THREE.Vector3(16, 1, 16), 0.06, false);
  if (pool) {
    lamps.forEach((k, t) => {
      const r = arr[k * 5 + 4];
      p.set(arr[k * 5] + Math.sin(r) * 1.3, arr[k * 5 + 1] + 0.06, arr[k * 5 + 2] + Math.cos(r) * 1.3);
      pool.setMatrixAt(t, m.compose(p, q.identity(), new THREE.Vector3(16, 1, 16)));
    });
    pool.renderOrder = 2;
  }
  for (const geo of PROP_GEO.shelterFrame) inst(geo, metal, stops);
  inst(PROP_GEO.shelterGlass, glassMat, stops, one, 0, false);
  for (const geo of PROP_GEO.signal) inst(geo, signalMat, signals);
  return g;
}

// ---------------------------------------------------------------- svět
// Dohledy (m). Vrstvy se načítají jen pro dlaždice v záběru kamery (frustum + okraj),
// mazání má větší poloměr než načítání (hystereze → žádné cukání na hranici).
const FOG_FAR = 14000;        // FogExp2 0.00011: ve 14 km je mlha ~90 %, dál nemá smysl nic tahat
const VIEW_MARGIN = 150;      // okraj kolem dlaždice pro test záběru při načítání
const KEEP_MARGIN = 400;      // volnější okraj – rozběhnuté stahování se neruší při malém pootočení
const HI_LOAD = 2000, HI_KEEP = 2600;  // detailní textura terénu
// [klíč v busy, vlastnost dlaždice, počet v meta, poloměr načtení, poloměr mazání, priorita]
const LAYERS = [
  ['r', 'roads', 'r', 2300, 2900, 300],
  ['b', 'buildings', 'b', 5000, 6200, 300],
  ['t', 'trees', 't', 2600, 3300, 700],
  ['p', 'props', 'p', 1600, 2100, 700],
];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const _m = new THREE.Matrix4(), _fr = new THREE.Frustum(), _box = new THREE.Box3();

export class World {
  constructor(scene, meta) {
    this.scene = scene;
    this.meta = meta;
    // stabilní verze dat → prohlížeč smí dlaždice brát z HTTP cache
    this.ver = meta.version != null ? `?v=${meta.version}` : '';
    this.bMat = buildingMaterial();
    this.rMat = new THREE.MeshLambertMaterial({ vertexColors: true });
    this.onChange = () => {};
    const [x0, , , y1] = meta.extent;
    const [ox, oy] = meta.origin;
    this.res = meta.chunk / meta.terrainN;
    this.chunks = meta.chunks.map((c) => ({
      ...c,
      wx: x0 + c.i * meta.chunk - ox,
      wz: (oy - y1) + c.j * meta.chunk,
      h16: null, hmin: null, hmax: null, terrain: null, lod: 0, tLoading: 0,
      buildings: null, trees: null, roads: null, props: null,
      busy: new Map(),   // jméno úlohy → AbortController
      failed: new Set(), // dlaždice, které na serveru nejsou (neopakovat donekonečna)
    }));
    this.byIJ = new Map(this.chunks.map((c) => [`${c.i}_${c.j}`, c]));
    this.group = new THREE.Group();
    scene.add(this.group);
    this.pending = 0;
  }

  url(dir, c, ext) { return `${DATA}/${dir}/${c.i}_${c.j}.${ext}${this.ver}`; }

  // index dlaždice (i, j) pro světové souřadnice
  ij(x, z) {
    const C = this.meta.chunk, c0 = this.chunks[0];
    return [Math.floor((x - (c0.wx - c0.i * C)) / C), Math.floor((z - (c0.wz - c0.j * C)) / C)];
  }

  chunkAt(x, z) {
    const [i, j] = this.ij(x, z);
    return this.byIJ.get(`${i}_${j}`);
  }

  heightAt(x, z) {
    const c = this.chunkAt(x, z);
    if (!c || !c.h16) return null;
    const N = this.meta.terrainN;
    const fl = Math.min(N - 1e-6, Math.max(0, (x - c.wx) / this.res));
    const fk = Math.min(N - 1e-6, Math.max(0, (z - c.wz) / this.res));
    const l = Math.floor(fl), k = Math.floor(fk), fx = fl - l, fz = fk - k;
    const H = (kk, ll) => c.h16[kk * (N + 1) + ll] / 20;
    const a = H(k, l), b = H(k + 1, l), cc = H(k + 1, l + 1), d = H(k, l + 1);
    return fx + fz <= 1 ? a + (d - a) * fx + (b - a) * fz : cc + (b - cc) * (1 - fx) + (d - cc) * (1 - fz);
  }

  // Úloha s vlastním AbortControllerem; chyby (chybějící dlaždice, přerušení) nikdy neuniknou.
  job(c, name, fn) {
    if (c.busy.has(name)) return;
    const ac = new AbortController();
    c.busy.set(name, ac); this.pending++;
    (async () => {
      try { await fn(ac.signal); } catch (e) {
        if (!ac.signal.aborted && e?.missing) c.failed.add(name);  // jen 404, ne výpadek sítě
      } finally {
        if (c.busy.get(name) === ac) c.busy.delete(name);
        this.pending--;
      }
    })();
  }

  // Zruší rozběhnutou úlohu; doběhnuvší výsledek se díky signal.aborted nepřidá do scény.
  cancel(c, name) {
    const ac = c.busy.get(name);
    if (!ac) return;
    ac.abort();
    c.busy.delete(name);
    if (name === 'terrain') c.tLoading = 0;
  }

  startTerrain(c, lod) {
    c.tLoading = lod;
    this.job(c, 'terrain', async (sig) => {
      try { await this.loadTerrain(c, lod, sig); } catch (e) {
        if (!sig.aborted && e?.missing) c.failed.add('terrain' + lod);
        throw e;
      } finally { if (!sig.aborted) c.tLoading = 0; }
    });
  }

  async loadTerrain(c, lod, signal) {
    if (!c.h16) {
      const h = new Uint16Array(await fetchBuf(this.url('terrain', c, 'bin'), signal));
      let lo = Infinity, hi = -Infinity;
      for (let k = 0; k < h.length; k++) { const v = h[k]; if (v < lo) lo = v; if (v > hi) hi = v; }
      c.h16 = h; c.hmin = lo / 20; c.hmax = hi / 20;  // výšky zůstávají v paměti (heightAt)
    }
    if (signal.aborted) throw aborted();
    const hi = lod === 1;
    const [g, tex] = await Promise.allSettled([
      work('terrain', { h16: c.h16, N: this.meta.terrainN, res: this.res, stride: hi ? 1 : 5, wx: c.wx, wz: c.wz }),
      loadTex(this.url(hi ? 'tex' : 'tex_lo', c, 'jpg'), signal),
    ]);
    if (g.status !== 'fulfilled' || tex.status !== 'fulfilled' || signal.aborted) {
      if (tex.status === 'fulfilled') tex.value.dispose();
      throw signal.aborted ? aborted() : (g.reason || tex.reason);
    }
    const r = g.value;
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(r.pos, 3));
    geo.setAttribute('normal', new THREE.BufferAttribute(r.nrm, 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(r.uv, 2));
    geo.setIndex(new THREE.BufferAttribute(r.idx, 1));
    geo.computeBoundingSphere();
    const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ map: tex.value }));
    mesh.receiveShadow = true;
    this.dropTerrain(c, false);
    c.terrain = mesh; c.lod = lod;
    this.group.add(mesh);
    this.onChange();
  }

  async loadMesh(c, kind, mat, shadow, signal) {
    const buf = await fetchBuf(this.url(kind, c, 'bin'), signal);
    const g = await work('mesh', { buf }, [buf]);
    if (signal.aborted) throw aborted();  // dlaždice mezitím vypadla z potřebných
    const mesh = new THREE.Mesh(meshGeometry(g), mat);
    mesh.castShadow = shadow;
    mesh.receiveShadow = true;
    c[kind] = mesh;
    this.group.add(mesh);
    this.onChange();
  }

  async loadProps(c, signal) {
    const buf = await fetchBuf(this.url('props', c, 'bin'), signal);
    if (signal.aborted) throw aborted();
    c.props = propsGroup(new Float32Array(buf));
    this.group.add(c.props);
    this.onChange();
  }

  async loadTrees(c, signal) {
    const buf = await fetchBuf(this.url('trees', c, 'bin'), signal);
    const t = await work('trees', { buf }, [buf]);
    if (signal.aborted) throw aborted();
    const g = new THREE.Group();
    for (const im of [instanced(crownGeo, treeMat, t.dm, t.dc), instanced(coneGeo, treeMat, t.cm, t.cc),
      instanced(trunkGeo, trunkMat, t.tm)]) if (im) g.add(im);
    c.trees = g;
    this.group.add(g);
    this.onChange();
  }

  drop(c, key) {
    const o = c[key];
    if (!o) return;
    this.group.remove(o);
    o.traverse((m) => {
      if (m.geometry && !SHARED.has(m.geometry) && !PROP_SHARED.has(m.geometry)) m.geometry.dispose();
      if (m.isInstancedMesh) m.dispose();
    });
    c[key] = null;
    this.onChange();
  }

  // Odebere mesh terénu (výšky h16 zůstávají v paměti).
  dropTerrain(c, notify = true) {
    const t = c.terrain;
    if (!t) return;
    this.group.remove(t);
    t.geometry.dispose(); t.material.map?.dispose(); t.material.dispose();
    c.terrain = null; c.lod = 0;
    if (notify) this.onChange();
  }

  // Je dlaždice (rozšířená o okraj) v záběru kamery?
  inView(c, fr, margin) {
    const C = this.meta.chunk;
    // svislý rozsah: známé výšky terénu + budovy/stromy, jinak velkoryse
    const y0 = c.hmin != null ? c.hmin - 30 : -300;
    const y1 = c.hmax != null ? c.hmax + 150 : 2500;
    _box.min.set(c.wx - margin, y0 - margin, c.wz - margin);
    _box.max.set(c.wx + C + margin, y1 + margin, c.wz + C + margin);
    return fr.intersectsBox(_box);
  }

  // Volá se ~5× za sekundu. camera je volitelná (bez ní se frustum nepoužije).
  update(target, maxJobs = 4, camera = null) {
    const C = this.meta.chunk;
    const cam = camera ? camera.position : target;
    let fr = null;
    if (camera) {
      camera.updateMatrixWorld();
      fr = _fr.setFromProjectionMatrix(_m.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse));
    }
    // Dohled podle výšky kamery nad terénem a vzdálenosti od cíle (nízká kamera vidí méně,
    // za ~14 km vše pohltí mlha). Budovy se při blízkém pohledu (auto) berou z menšího okruhu.
    const ground = this.heightAt(cam.x, cam.z) ?? this.heightAt(target.x, target.z) ?? 0;
    const hAG = Math.max(0, cam.y - ground);
    const camDist = camera ? cam.distanceTo(target) : 1000;
    const terrR = clamp(7000 + 4 * hAG + camDist, 7000, FOG_FAR);
    const terrDrop = terrR * 1.3;
    const bLoad = clamp(2500 + 2 * camDist + hAG, 2500, 5000);
    // dlaždice pod cílem a pod kamerou + sousedé: vždy terén (heightAt), nezávisle na záběru
    const [ti, tj] = this.ij(target.x, target.z), [ci, cj] = this.ij(cam.x, cam.z);
    const isNear = (c) => (Math.abs(c.i - ti) <= 1 && Math.abs(c.j - tj) <= 1) ||
      (Math.abs(c.i - ci) <= 1 && Math.abs(c.j - cj) <= 1);

    const jobs = [];
    for (const c of this.chunks) {
      const d = Math.hypot(c.wx + C / 2 - target.x, c.wz + C / 2 - target.z);
      // vzdálenost kamery k nejbližšímu bodu dlaždice
      const dc = Math.hypot(Math.max(c.wx - cam.x, 0, cam.x - c.wx - C), Math.max(c.wz - cam.z, 0, cam.z - c.wz - C));
      const near = isNear(c);
      const vis = !fr || this.inView(c, fr, VIEW_MARGIN);
      const keepVis = vis || this.inView(c, fr, KEEP_MARGIN);
      const w = vis ? 1 : near ? 1.5 : 4;  // v záběru dřív, mimo záběr (jen okolí) později

      // --- terén
      if (near || (vis && dc < terrR)) {
        let want = (vis && d < HI_LOAD) || ((c.lod === 1 || c.tLoading === 1) && d < HI_KEEP) ? 1 : 2;
        if (want === 1 && c.failed.has('terrain1')) want = 2;  // chybí detailní textura → aspoň hrubá
        if (c.busy.has('terrain') && c.tLoading !== want) this.cancel(c, 'terrain');
        if (c.lod !== want && !c.busy.has('terrain') && !c.failed.has('terrain' + want)) {
          jobs.push({ s: d * w + (want === 1 && c.lod ? 200 : 0), run: () => this.startTerrain(c, want) });
        }
      } else {
        if (c.busy.has('terrain') && !(keepVis && dc < terrDrop)) this.cancel(c, 'terrain');
        if (c.terrain && dc > terrDrop) this.dropTerrain(c);
        else if (c.lod === 1 && d > HI_KEEP && !c.busy.has('terrain') && !c.failed.has('terrain2')) {
          // mimo záběr, ale ještě v dohledu: zpět na hrubou texturu (z HTTP cache)
          jobs.push({ s: d * w + 900, run: () => this.startTerrain(c, 2) });
        }
      }

      // --- budovy, silnice, stromy, mobiliář
      for (const [key, prop, cnt, rLoad0, rDrop0, pri] of LAYERS) {
        if (!c[cnt]) continue;
        const rLoad = key === 'b' ? bLoad : rLoad0;
        const rDrop = key === 'b' ? bLoad + 1200 : rDrop0;
        if (c[prop]) { if (d > rDrop) this.drop(c, prop); continue; }
        if (c.busy.has(key)) {
          if (d > rDrop || !(keepVis || near)) this.cancel(c, key);
          continue;
        }
        if (d < rLoad && (vis || near) && !c.failed.has(key)) {
          const run = key === 'r' ? (sig) => this.loadMesh(c, 'roads', this.rMat, false, sig)
            : key === 'b' ? (sig) => this.loadMesh(c, 'buildings', this.bMat, true, sig)
              : key === 't' ? (sig) => this.loadTrees(c, sig) : (sig) => this.loadProps(c, sig);
          jobs.push({ s: d * w + pri, run: () => this.job(c, key, run) });
        }
      }
    }
    jobs.sort((a, b) => a.s - b.s);
    for (const j of jobs) {
      if (this.pending >= maxJobs) break;
      j.run();
    }
  }
}
