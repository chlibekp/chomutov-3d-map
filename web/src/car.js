import * as THREE from 'three';
import { DATA } from './region.js';

// Jízda autem: arkádová fyzika, kolize s budovami (půdorysy), mosty, kamera za autem.

const CELL = 50;
const key = (x, z) => `${Math.floor(x / CELL)},${Math.floor(z / CELL)}`;

function gridAdd(grid, minx, minz, maxx, maxz, val) {
  for (let i = Math.floor(minx / CELL); i <= Math.floor(maxx / CELL); i++) {
    for (let j = Math.floor(minz / CELL); j <= Math.floor(maxz / CELL); j++) {
      const k = `${i},${j}`;
      (grid.get(k) || grid.set(k, []).get(k)).push(val);
    }
  }
}

function pointInRing(x, z, r) {
  let inside = false;
  for (let a = 0, b = r.length / 2 - 1; a < r.length / 2; b = a++) {
    const xa = r[a * 2], za = r[a * 2 + 1], xb = r[b * 2], zb = r[b * 2 + 1];
    if ((za > z) !== (zb > z) && x < ((xb - xa) * (z - za)) / (zb - za) + xa) inside = !inside;
  }
  return inside;
}

// vzdálenost bodu od úsečky + parametr t
function segDist(px, pz, ax, az, bx, bz) {
  const dx = bx - ax, dz = bz - az;
  const L = dx * dx + dz * dz || 1;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (pz - az) * dz) / L));
  return [Math.hypot(px - ax - dx * t, pz - az - dz * t), t];
}

function makeCar(color = 0xc2452d) {
  const g = new THREE.Group();
  const paint = new THREE.MeshStandardMaterial({ color, roughness: 0.45, metalness: 0.2, flatShading: true });
  const glass = new THREE.MeshStandardMaterial({ color: 0x1c2630, roughness: 0.15, metalness: 0.5 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x1a1a1a, roughness: 0.9 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.56, 4.2), paint);
  body.position.y = 0.74;
  const skirt = new THREE.Mesh(new THREE.BoxGeometry(1.4, 0.3, 3.9), dark);
  skirt.position.y = 0.42;
  const cabin = new THREE.Mesh(new THREE.BoxGeometry(1.56, 0.56, 2.1), glass);
  cabin.position.set(0, 1.3, -0.25);
  const roof = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.08, 1.8), paint);
  roof.position.set(0, 1.6, -0.3);
  const bumperF = new THREE.Mesh(new THREE.BoxGeometry(1.84, 0.22, 0.12), dark);
  bumperF.position.set(0, 0.5, 2.12);
  const bumperR = bumperF.clone();
  bumperR.position.z = -2.12;
  g.add(body, skirt, cabin, roof, bumperF, bumperR);

  const headMat = new THREE.MeshStandardMaterial({ color: 0xfff4d6, emissive: 0xfff0c8, emissiveIntensity: 0.4 });
  const tailMat = new THREE.MeshStandardMaterial({ color: 0x8a1010, emissive: 0xff2010, emissiveIntensity: 0.3 });
  for (const sx of [-0.62, 0.62]) {
    const h = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.14, 0.05), headMat);
    h.position.set(sx, 0.82, 2.11);
    const t = new THREE.Mesh(new THREE.BoxGeometry(0.4, 0.14, 0.05), tailMat);
    t.position.set(sx, 0.85, -2.11);
    g.add(h, t);
  }

  const wheelGeo = new THREE.CylinderGeometry(0.34, 0.34, 0.26, 14);
  wheelGeo.rotateZ(Math.PI / 2);
  const wheels = [];
  for (const [x, z] of [[-0.84, 1.32], [0.84, 1.32], [-0.84, -1.3], [0.84, -1.3]]) {
    const pivot = new THREE.Group();
    pivot.position.set(x, 0.34, z);
    const w = new THREE.Mesh(wheelGeo, dark);
    pivot.add(w);
    g.add(pivot);
    wheels.push({ pivot, w, front: z > 0 });
  }
  g.traverse((m) => { if (m.isMesh) { m.castShadow = true; } });

  // světlomety (v noci)
  const spot = new THREE.SpotLight(0xfff0d0, 0, 90, 0.5, 0.6, 1.2);
  spot.position.set(0, 0.8, 2.0);
  spot.target.position.set(0, 0, 14);
  g.add(spot, spot.target);
  return { g, wheels, headMat, tailMat, spot };
}

export class Drive {
  constructor({ scene, camera, world, uniforms }) {
    this.scene = scene; this.camera = camera; this.world = world; this.uniforms = uniforms;
    this.active = false;
    this.car = makeCar();
    this.car.g.visible = false;
    scene.add(this.car.g);
    this.pos = new THREE.Vector3();
    this.heading = 0; this.speed = 0; this.steer = 0; this.y = 0;
    this.camMode = 0;
    this.camPos = new THREE.Vector3();
    this.camLook = new THREE.Vector3();
    this.loaded = null;
    this.streetT = 0;
    this.hud = document.getElementById('hud');
  }

  load() {
    this.loaded ??= (async () => {
      const [fp, dj] = await Promise.all([
        fetch(`${DATA}/drive_fp.bin`).then((r) => r.arrayBuffer()),
        fetch(`${DATA}/drive.json`).then((r) => r.json()),
      ]);
      const n = new Uint32Array(fp, 0, 1)[0];
      const offs = new Uint32Array(fp, 4, n + 1);
      const xy = new Float32Array(fp, 4 + (n + 1) * 4);
      this.rings = [];
      this.fpGrid = new Map();
      for (let k = 0; k < n; k++) {
        const r = xy.subarray(offs[k] * 2, offs[k + 1] * 2);
        let minx = Infinity, minz = Infinity, maxx = -Infinity, maxz = -Infinity;
        for (let q = 0; q < r.length; q += 2) {
          minx = Math.min(minx, r[q]); maxx = Math.max(maxx, r[q]);
          minz = Math.min(minz, r[q + 1]); maxz = Math.max(maxz, r[q + 1]);
        }
        this.rings.push({ r, minx, minz, maxx, maxz });
        gridAdd(this.fpGrid, minx, minz, maxx, maxz, k);
      }
      this.streets = dj.streets;
      this.stGrid = new Map();
      dj.streets.forEach(([, c], si) => {
        for (let q = 0; q + 3 < c.length; q += 2) {
          gridAdd(this.stGrid, Math.min(c[q], c[q + 2]) - 20, Math.min(c[q + 1], c[q + 3]) - 20,
            Math.max(c[q], c[q + 2]) + 20, Math.max(c[q + 1], c[q + 3]) + 20, [si, q]);
        }
      });
      this.bridges = dj.bridges;
      this.brGrid = new Map();
      dj.bridges.forEach(([hw, c], bi) => {
        for (let q = 0; q + 5 < c.length; q += 3) {
          gridAdd(this.brGrid, Math.min(c[q], c[q + 3]) - hw, Math.min(c[q + 2], c[q + 5]) - hw,
            Math.max(c[q], c[q + 3]) + hw, Math.max(c[q + 2], c[q + 5]) + hw, [bi, q]);
        }
      });
    })();
    return this.loaded;
  }

  nearestStreet(x, z, maxD = 30) {
    let best = null, bd = maxD;
    for (const [si, q] of this.stGrid.get(key(x, z)) || []) {
      const c = this.streets[si][1];
      const [d] = segDist(x, z, c[q], c[q + 1], c[q + 2], c[q + 3]);
      if (d < bd) { bd = d; best = { si, q, d }; }
    }
    return best;
  }

  groundAt(x, z, prevY) {
    let y = this.world.heightAt(x, z);
    if (y === null) y = prevY ?? 0;
    y += 0.3; // povrch vozovky
    for (const [bi, q] of this.brGrid.get(key(x, z)) || []) {
      const [hw, c] = this.bridges[bi];
      const [d, t] = segDist(x, z, c[q], c[q + 2], c[q + 3], c[q + 5]);
      if (d > hw) continue;
      const bz = c[q + 1] + (c[q + 4] - c[q + 1]) * t;
      // most jen když na něm auto opravdu je (ne když projíždí pod ním)
      if (bz > y && (prevY === undefined || Math.abs(prevY - bz) < 2.5)) y = bz;
    }
    return y;
  }

  hits(x, z) {
    for (const k of this.fpGrid.get(key(x, z)) || []) {
      const R = this.rings[k];
      if (x < R.minx || x > R.maxx || z < R.minz || z > R.maxz) continue;
      if (pointInRing(x, z, R.r)) return true;
    }
    return false;
  }

  collides(x, z, h) {
    const s = Math.sin(h), c = Math.cos(h);
    for (const [lx, lz] of [[0.85, 2.0], [-0.85, 2.0], [0.85, -2.0], [-0.85, -2.0], [0, 2.1], [0, -2.1]]) {
      if (this.hits(x + lx * c + lz * s, z - lx * s + lz * c)) return true;
    }
    return false;
  }

  // postaví auto na nejbližší silnici k bodu
  async place(p) {
    await this.load();
    let x = p.x, z = p.z, h = this.heading;
    let best = null, bd = 400;
    const R = Math.ceil(400 / CELL), ci = Math.floor(x / CELL), cj = Math.floor(z / CELL);
    for (let i = ci - R; i <= ci + R; i++) {
      for (let j = cj - R; j <= cj + R; j++) {
        for (const [si, q] of this.stGrid.get(`${i},${j}`) || []) {
          const c = this.streets[si][1];
          const [d, t] = segDist(x, z, c[q], c[q + 1], c[q + 2], c[q + 3]);
          if (d < bd) { bd = d; best = { c, q, t }; }
        }
      }
    }
    if (best) {
      const { c, q, t } = best;
      x = c[q] + (c[q + 2] - c[q]) * t;
      z = c[q + 1] + (c[q + 3] - c[q + 1]) * t;
      h = Math.atan2(c[q + 2] - c[q], c[q + 3] - c[q + 1]);
    } else { x = p.x; z = p.z; }
    this.pos.set(x, 0, z);
    this.heading = h; this.speed = 0; this.steer = 0;
    this.y = this.groundAt(x, z);
    this.pos.y = this.y;
    const fwd = new THREE.Vector3(Math.sin(h), 0, Math.cos(h));
    this.camPos.copy(this.pos).addScaledVector(fwd, -9).add(new THREE.Vector3(0, 4, 0));
    this.camLook.copy(this.pos);
  }

  async start(p) {
    await this.place(p);
    this.active = true;
    this.car.g.visible = true;
    this.hud.hidden = false;
    this.fov0 = this.camera.fov;
  }

  stop() {
    this.active = false;
    this.hud.hidden = true;
    this.camera.fov = this.fov0 ?? 40;
    this.camera.updateProjectionMatrix();
  }

  update(dt, keys) {
    const K = (...c) => c.some((k) => keys.has(k));
    const fwdIn = K('KeyW', 'ArrowUp'), back = K('KeyS', 'ArrowDown');
    const left = K('KeyA', 'ArrowLeft'), right = K('KeyD', 'ArrowRight');
    const hand = K('Space'), boost = K('ShiftLeft', 'ShiftRight');
    const VTOP = 300 / 3.6;                  // 300 km/h strop
    let braking = false;

    // podélná dynamika (boost: téměř konstantní tah až k 300 km/h)
    if (fwdIn) {
      const thrust = this.speed < 0 ? 14
        : boost ? 9 * (1 - Math.pow(this.speed / 104, 3))
        : 6.5 * (1 - this.speed / 25);     // 90 km/h bez boostu
      this.speed += thrust * dt;
    }
    if (back) {
      if (this.speed > 0.5) { this.speed -= 14 * dt; braking = true; }
      else this.speed = Math.max(-8, this.speed - 5 * dt);
    }
    if (hand) { this.speed *= Math.exp(-dt * 2.5); braking = true; }
    if (!fwdIn && !back) this.speed -= Math.sign(this.speed) * Math.min(Math.abs(this.speed), 1.2 * dt);
    this.speed *= Math.exp(-dt * 0.05);
    this.speed = Math.min(this.speed, VTOP);
    // mimo silnici pomaleji
    const onRoad = this.nearestStreet(this.pos.x, this.pos.z, 10);
    if (!onRoad && Math.abs(this.speed) > 9) this.speed *= Math.exp(-dt * 0.8);

    // řízení
    const want = (left ? 1 : 0) - (right ? 1 : 0);
    this.steer += (want - this.steer) * (1 - Math.exp(-dt * (want ? 4 : 7)));
    const maxSteer = 0.6 / (1 + Math.abs(this.speed) / 14);
    const yaw = (this.speed / 2.6) * Math.tan(this.steer * maxSteer) * (hand ? 1.6 : 1);
    const nh = this.heading + yaw * dt;
    const nx = this.pos.x + Math.sin(nh) * this.speed * dt;
    const nz = this.pos.z + Math.cos(nh) * this.speed * dt;

    if (this.collides(nx, nz, nh)) {
      this.speed *= -0.25;           // odraz od zdi
      this.shake = 0.35;
    } else {
      this.pos.x = nx; this.pos.z = nz; this.heading = nh;
    }

    // výška, náklon podle terénu
    const h = this.heading, s = Math.sin(h), c = Math.cos(h);
    const gy = this.groundAt(this.pos.x, this.pos.z, this.y);
    this.y += (gy - this.y) * (1 - Math.exp(-dt * 12));
    const yf = this.groundAt(this.pos.x + s * 1.4, this.pos.z + c * 1.4, this.y);
    const yb = this.groundAt(this.pos.x - s * 1.4, this.pos.z - c * 1.4, this.y);
    const yl = this.groundAt(this.pos.x + c * 0.8, this.pos.z - s * 0.8, this.y);
    const yr = this.groundAt(this.pos.x - c * 0.8, this.pos.z + s * 0.8, this.y);
    const pitch = -Math.atan2(yf - yb, 2.8);
    const roll = Math.atan2(yl - yr, 1.6) - this.steer * Math.min(1, Math.abs(this.speed) / 20) * 0.05;
    this.pos.y = this.y;

    const g = this.car.g;
    g.position.copy(this.pos);
    g.rotation.set(pitch, h, roll, 'YXZ');
    for (const w of this.car.wheels) {
      w.w.rotation.x += (this.speed / 0.34) * dt;
      if (w.front) w.pivot.rotation.y = this.steer * maxSteer;
    }
    const night = this.uniforms.uNight.value;
    this.car.spot.intensity = night * 60;
    this.car.headMat.emissiveIntensity = 0.3 + night * 2.5;
    this.car.tailMat.emissiveIntensity = braking ? 3 : 0.3 + night * 0.8;

    // kamera
    const fwd = new THREE.Vector3(s, 0, c);
    const v = Math.abs(this.speed);
    let cp, look;
    if (this.camMode === 1) {             // z kapoty
      cp = this.pos.clone().addScaledVector(fwd, 0.4).add(new THREE.Vector3(0, 1.45, 0));
      look = this.pos.clone().addScaledVector(fwd, 20).add(new THREE.Vector3(0, 1.2, 0));
      this.camPos.copy(cp); this.camLook.copy(look);
    } else {
      const far = this.camMode === 2 ? 2.6 : 1;
      cp = this.pos.clone().addScaledVector(fwd, -(8 + v * 0.12) * far).add(new THREE.Vector3(0, (3.2 + v * 0.03) * far * far, 0));
      look = this.pos.clone().addScaledVector(fwd, 4).add(new THREE.Vector3(0, 1.2, 0));
      this.camPos.lerp(cp, 1 - Math.exp(-dt * 5));
      this.camLook.lerp(look, 1 - Math.exp(-dt * 10));
    }
    const ct = this.world.heightAt(this.camPos.x, this.camPos.z);
    if (ct !== null && this.camPos.y < ct + 1.5) this.camPos.y = ct + 1.5;
    this.camera.position.copy(this.camPos);
    if (this.shake > 0) {
      this.shake -= dt;
      this.camera.position.x += (Math.random() - 0.5) * this.shake;
      this.camera.position.y += (Math.random() - 0.5) * this.shake;
    }
    this.camera.lookAt(this.camLook);
    const fov = 50 + v * 0.45;
    if (Math.abs(this.camera.fov - fov) > 0.1) {
      this.camera.fov += (fov - this.camera.fov) * (1 - Math.exp(-dt * 3));
      this.camera.updateProjectionMatrix();
    }

    // HUD
    this.streetT -= dt;
    if (this.streetT <= 0) {
      this.streetT = 0.3;
      const st = this.nearestStreet(this.pos.x, this.pos.z, 25);
      this.hud.querySelector('.street').textContent = st ? this.streets[st.si][0] : '';
    }
    this.hud.querySelector('.speed').textContent = Math.round(v * 3.6);
    this.hud.querySelector('.gear').textContent = this.speed < -0.3 ? 'R' : 'D';
  }
}
