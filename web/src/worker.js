// Stavba geometrie mimo hlavní vlákno → plynulý pohyb během načítání dlaždic.

function terrain({ h16, N, res, stride, wx, wz }) {
  const n = N / stride;
  const step = stride * res;
  const nv = (n + 1) * (n + 1);
  const pos = new Float32Array(nv * 3);
  const uv = new Float32Array(nv * 2);
  const nrm = new Float32Array(nv * 3);
  const H = (k, l) => h16[(Math.min(n, Math.max(0, k)) * stride) * (N + 1) + Math.min(n, Math.max(0, l)) * stride] / 20;
  for (let k = 0; k <= n; k++) {
    for (let l = 0; l <= n; l++) {
      const v = k * (n + 1) + l;
      pos[v * 3] = wx + l * step;
      pos[v * 3 + 1] = H(k, l);
      pos[v * 3 + 2] = wz + k * step;
      uv[v * 2] = l / n;
      uv[v * 2 + 1] = 1 - k / n;
      // normála z centrálních diferencí (bez švů mezi trojúhelníky)
      const dx = (H(k, l + 1) - H(k, l - 1)) / ((Math.min(n, l + 1) - Math.max(0, l - 1)) * step);
      const dz = (H(k + 1, l) - H(k - 1, l)) / ((Math.min(n, k + 1) - Math.max(0, k - 1)) * step);
      const il = 1 / Math.hypot(dx, 1, dz);
      nrm[v * 3] = -dx * il;
      nrm[v * 3 + 1] = il;
      nrm[v * 3 + 2] = -dz * il;
    }
  }
  const idx = new Uint32Array(n * n * 6);
  let t = 0;
  for (let k = 0; k < n; k++) for (let l = 0; l < n; l++) {
    const a = k * (n + 1) + l, b = a + n + 1, c = b + 1, d = a + 1;
    idx[t++] = a; idx[t++] = b; idx[t++] = d;
    idx[t++] = b; idx[t++] = c; idx[t++] = d;
  }
  return { result: { pos, uv, nrm, idx }, transfer: [pos.buffer, uv.buffer, nrm.buffer, idx.buffer] };
}

function mesh({ buf }) {
  const n = new Uint32Array(buf, 0, 1)[0];
  const pos = new Float32Array(buf, 4, n * 3).slice();
  const uv = new Float32Array(buf, 4 + n * 12, n * 2).slice();
  const col = new Uint8Array(buf, 4 + n * 20, n * 4).slice();
  const nrm = new Float32Array(n * 3);
  // ploché normály (geometrie je neindexovaná)
  for (let t = 0; t < n; t += 3) {
    const a = t * 3, b = a + 3, c = a + 6;
    const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2];
    const vx = pos[c] - pos[a], vy = pos[c + 1] - pos[a + 1], vz = pos[c + 2] - pos[a + 2];
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const il = 1 / (Math.hypot(nx, ny, nz) || 1);
    nx *= il; ny *= il; nz *= il;
    for (let q = 0; q < 3; q++) { nrm[a + q * 3] = nx; nrm[a + q * 3 + 1] = ny; nrm[a + q * 3 + 2] = nz; }
  }
  return { result: { pos, uv, col, nrm }, transfer: [pos.buffer, uv.buffer, col.buffer, nrm.buffer] };
}

const DECID = [0x8b5a2b, 0xa0602d, 0xb08a2e, 0x6e6a66, 0x7a3b2e, 0x8f7a3a, 0x5d5853, 0x9a4a32, 0x6f7a3a, 0x58604a];
const CONIF = [0x2f4a36, 0x2a4232, 0x35553c, 0x3e5b3f];
const lin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const rgb = (hex) => [lin(((hex >> 16) & 255) / 255), lin(((hex >> 8) & 255) / 255), lin((hex & 255) / 255)];
const DECID_L = DECID.map(rgb), CONIF_L = CONIF.map(rgb);
const rnd = (i) => { const x = Math.sin(i * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

function setM(m, o, x, y, z, rot, sx, sy, sz) {
  const c = Math.cos(rot), s = Math.sin(rot);
  m.set([c * sx, 0, -s * sx, 0, 0, sy, 0, 0, s * sz, 0, c * sz, 0, x, y, z, 1], o * 16);
}

function trees({ buf }) {
  const arr = new Float32Array(buf);
  const n = arr.length / 6;
  let nd = 0, nc = 0;
  for (let k = 0; k < n; k++) (arr[k * 6 + 5] > 0.5 ? nc++ : nd++);
  const dm = new Float32Array(nd * 16), dc = new Float32Array(nd * 3);
  const cm = new Float32Array(nc * 16), cc = new Float32Array(nc * 3);
  const tm = new Float32Array(n * 16);
  let id = 0, ic = 0;
  for (let k = 0; k < n; k++) {
    const x = arr[k * 6], y = arr[k * 6 + 1], z = arr[k * 6 + 2];
    const h = arr[k * 6 + 3], r = arr[k * 6 + 4], conif = arr[k * 6 + 5] > 0.5;
    const rs = rnd(x * 0.13 + z * 0.71);
    const rot = rs * 6.28;
    if (conif) {
      const base = h * 0.12;
      setM(cm, ic, x, y + base, z, rot, r * 0.7, h - base, r * 0.7);
      cc.set(CONIF_L[Math.floor(rs * CONIF.length)], ic * 3); ic++;
      setM(tm, k, x, y, z, rot, 0.35, base + 0.5, 0.35);
    } else {
      const cr = Math.min(r, h * 0.45);
      setM(dm, id, x, y + h - cr * 0.95, z, rot, cr, cr * 1.05, cr);
      dc.set(DECID_L[Math.floor(rs * DECID.length)], id * 3); id++;
      const tw = 0.3 + h * 0.012;
      setM(tm, k, x, y, z, rot, tw, Math.max(h - cr * 1.4, 0.5), tw);
    }
  }
  return { result: { dm, dc, cm, cc, tm }, transfer: [dm.buffer, dc.buffer, cm.buffer, cc.buffer, tm.buffer] };
}

const JOBS = { terrain, mesh, trees };

self.onmessage = (e) => {
  const { id, type, args } = e.data;
  try {
    const { result, transfer } = JOBS[type](args);
    self.postMessage({ id, result }, transfer);
  } catch (err) {
    self.postMessage({ id, error: String(err) });
  }
};
