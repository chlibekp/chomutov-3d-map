import * as THREE from 'three';
import { MapControls } from 'three/addons/controls/MapControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { World, uniforms, setAniso, updateProps } from './world.js';
import { initSearch } from './search.js';
import { Drive } from './car.js';
import { DATA, initRegionSelect } from './region.js';
import './style.css';

const app = document.getElementById('app');
const status = document.getElementById('status');

// výkon: na retině bez MSAA (pixely jsou dost malé); standardní depth buffer s adaptivní
// blízkou rovinou místo logaritmického (ten zapisuje gl_FragDepth → vypíná early-Z)
const hiDpi = devicePixelRatio >= 2;
const renderer = new THREE.WebGLRenderer({ antialias: !hiDpi });
renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
renderer.setSize(innerWidth, innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;
renderer.shadowMap.autoUpdate = false;   // stínová mapa jen při změně (viz shadowDirty)
renderer.toneMapping = THREE.ACESFilmicToneMapping;
app.appendChild(renderer.domElement);
setAniso(Math.min(8, renderer.capabilities.getMaxAnisotropy()));

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
Object.assign(labelRenderer.domElement.style, { position: 'fixed', inset: '0', pointerEvents: 'none' });
document.body.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x3a4656, 0.00011);
const camera = new THREE.PerspectiveCamera(40, innerWidth / innerHeight, 2, 40000);

const controls = new MapControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.maxPolarAngle = Math.PI * 0.46;
controls.minDistance = 25;
controls.maxDistance = 14000;
controls.zoomToCursor = true;
controls.zoomSpeed = 1.4;
controls.rotateSpeed = 0.5;
controls.panSpeed = 1.0;

// ---------------------------------------------------------------- světla
const hemi = new THREE.HemisphereLight(0xbfd0e0, 0x4a5240, 1.0);
scene.add(hemi);
const sun = new THREE.DirectionalLight(0xffe2b8, 2.0);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
const S = 900;
Object.assign(sun.shadow.camera, { left: -S, right: S, top: S, bottom: -S, near: 10, far: 6000 });
sun.shadow.bias = -0.0004;
sun.shadow.normalBias = 0.6;
scene.add(sun, sun.target);

const SKY = {
  day: new THREE.Color(0x9db4c8), dusk: new THREE.Color(0x4b5568), night: new THREE.Color(0x111823),
};
const tmpC = new THREE.Color();

function setTime(t) {
  // t: 0 = půlnoc, 0.5 = poledne
  const a = (t - 0.25) * Math.PI * 2;
  const elev = Math.sin(a);                  // -1..1
  const az = Math.PI * 0.2 + t * Math.PI * 2;
  const night = THREE.MathUtils.smoothstep(-elev, -0.05, 0.25);
  const dusk = 1 - Math.min(1, Math.abs(elev) / 0.35);
  uniforms.uNight.value = Math.max(night, dusk * 0.6);
  updateProps(uniforms.uNight.value);
  sun.userData.dir = new THREE.Vector3(Math.cos(az) * 0.6, Math.max(elev, 0.08), Math.sin(az) * 0.6).normalize();
  sun.intensity = 2.4 * THREE.MathUtils.smoothstep(elev, -0.02, 0.3);
  sun.color.setHSL(0.08, 0.6, 0.6 + 0.3 * Math.max(elev, 0));
  hemi.intensity = 0.25 + 0.9 * THREE.MathUtils.smoothstep(elev, -0.2, 0.4);
  tmpC.copy(SKY.night).lerp(SKY.dusk, THREE.MathUtils.smoothstep(elev, -0.3, 0.0))
    .lerp(SKY.day, THREE.MathUtils.smoothstep(elev, 0.05, 0.45));
  scene.background = tmpC.clone();
  scene.fog.color.copy(tmpC);
  renderer.toneMappingExposure = 0.9 + 0.3 * night;
  invalidate(); shadowDirty = true;
}

// ---------------------------------------------------------------- render na vyžádání
// Scéna se kreslí jen když se něco změnilo → v klidu GPU nepracuje.
let dirty = 3, shadowDirty = true;
function invalidate(n = 2) { dirty = Math.max(dirty, n); }

// ---------------------------------------------------------------- popisky
const LABEL_RANGE = { city: 30000, town: 30000, suburb: 6000, village: 7000, quarter: 3500, neighbourhood: 2500, hamlet: 2500, locality: 1500, poi: 1800 };
const labels = [];
const labelGroup = new THREE.Group();
scene.add(labelGroup);

function addLabels(list) {
  const seen = new Set();
  for (const L of list) {
    const key = L.name + Math.round(L.x / 200) + Math.round(L.z / 200);
    if (seen.has(key)) continue;
    seen.add(key);
    const div = document.createElement('div');
    const place = L.kind !== 'poi';
    div.className = `lbl ${place ? 'place' : ''} ${L.kind === 'town' || L.kind === 'city' ? 'town' : ''}`;
    div.textContent = L.name;
    const o = new CSS2DObject(div);
    o.position.set(L.x, L.y + (place ? 60 : 22), L.z);
    o.userData = { range: LABEL_RANGE[L.kind] ?? 1500, base: L.y, kind: L.kind };
    labelGroup.add(o);
    labels.push(o);
  }
}

const PRIO = { city: 0, town: 0, suburb: 1, village: 1, quarter: 2, neighbourhood: 2, hamlet: 3, locality: 4, poi: 3 };
const v3 = new THREE.Vector3();

function updateLabels() {
  // dosah podle vzdálenosti + rozprostření (bez překryvů na obrazovce)
  const camDist = camera.position.distanceTo(controls.target);
  const W = innerWidth, H = innerHeight;
  const cand = [];
  for (const o of labels) {
    o.visible = false;
    const d = o.position.distanceTo(camera.position);
    const scale = drive.active ? 0.06 : Math.max(1, camDist / 1500);   // v autě jen blízké popisky
    if (d > o.userData.range * scale) continue;
    v3.copy(o.position).project(camera);
    if (v3.z > 1 || Math.abs(v3.x) > 1.1 || Math.abs(v3.y) > 1.1) continue;
    cand.push({ o, d, x: (v3.x + 1) / 2 * W, y: (1 - v3.y) / 2 * H, p: PRIO[o.userData.kind] ?? 3 });
  }
  cand.sort((a, b) => a.p - b.p || a.d - b.d);
  const placed = [];
  for (const c of cand) {
    const w = c.o.element.textContent.length * (c.p === 0 ? 14 : 8) / 2 + 6, h = 12;
    if (placed.some((r) => Math.abs(r.x - c.x) < r.w + w && Math.abs(r.y - c.y) < r.h + h)) continue;
    placed.push({ x: c.x, y: c.y, w, h });
    c.o.visible = true;
  }
}

// ---------------------------------------------------------------- UI
const toggles = {};
for (const id of ['labels', 'shadows']) {
  const b = document.getElementById(id);
  toggles[id] = true;
  b.onclick = () => {
    toggles[id] = !toggles[id];
    b.classList.toggle('on', toggles[id]);
    labelGroup.visible = toggles.labels;
    labelRenderer.domElement.style.display = toggles.labels ? '' : 'none';
    sun.castShadow = toggles.shadows && !eco;
    invalidate(); shadowDirty = true;
  };
}
// úsporný režim: 30 FPS, nižší rozlišení, bez stínů
let eco = false;
const ecoBtn = document.getElementById('eco');
ecoBtn.onclick = () => {
  eco = !eco;
  ecoBtn.classList.toggle('on', eco);
  sun.castShadow = toggles.shadows && !eco;
  maxDpr = eco ? 1 : Math.min(devicePixelRatio, 1.5);
  dpr = maxDpr; renderer.setPixelRatio(dpr);
  invalidate(); shadowDirty = true;
};
const timeInput = document.getElementById('time');
timeInput.oninput = () => setTime(+timeInput.value);
setTime(+timeInput.value);

// ---------------------------------------------------------------- start
const region = await initRegionSelect();
const meta = await (await fetch(`${DATA}/meta.json`)).json();
document.querySelector('#ui .title').innerHTML = `${meta.name || region?.name || ''} <span>3D</span>`;
document.title = `${meta.name || 'Mapa'} 3D`;
const world = new World(scene, meta);
world.onChange = () => { invalidate(); shadowDirty = true; };
controls.addEventListener('change', () => invalidate());
addLabels(await (await fetch(`${DATA}/labels.json`)).json());

const [sx, sy, sz] = meta.start;
controls.target.set(sx, sy, sz);
camera.position.set(sx - 650, sy + 420, sz + 650);
controls.update();
window.__map = { camera, controls, world, setTime };

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  labelRenderer.setSize(innerWidth, innerHeight);
  invalidate();
});

// ---------------------------------------------------------------- pohyb
const keys = new Set();
addEventListener('keydown', (e) => {
  if (e.target.closest?.('input')) return;
  keys.add(e.code);
  if (drive.active) {
    if (e.code === 'Space' || e.code.startsWith('Arrow')) e.preventDefault();
    if (e.code === 'KeyC') drive.camMode = (drive.camMode + 1) % 3;
    if (e.code === 'Escape') stopDrive();
  }
});
addEventListener('focusin', () => keys.clear());
addEventListener('keyup', (e) => keys.delete(e.code));
addEventListener('blur', () => keys.clear());
const vel = new THREE.Vector3();     // rychlost posunu (m/s)
let rotVel = 0, tiltVel = 0;
const fwd = new THREE.Vector3(), right = new THREE.Vector3(), off = new THREE.Vector3();

function keyboard(dt) {
  const dist = camera.position.distanceTo(controls.target);
  const speed = dist * (keys.has('ShiftLeft') || keys.has('ShiftRight') ? 2.5 : 1.0);
  camera.getWorldDirection(fwd); fwd.y = 0; fwd.normalize();
  right.crossVectors(fwd, camera.up).normalize();
  const want = new THREE.Vector3();
  if (keys.has('KeyW') || keys.has('ArrowUp')) want.add(fwd);
  if (keys.has('KeyS') || keys.has('ArrowDown')) want.sub(fwd);
  if (keys.has('KeyD') || keys.has('ArrowRight')) want.add(right);
  if (keys.has('KeyA') || keys.has('ArrowLeft')) want.sub(right);
  if (want.lengthSq()) want.normalize().multiplyScalar(speed);
  vel.lerp(want, 1 - Math.exp(-dt * 8));                  // plynulý rozjezd/dojezd
  const wantRot = (keys.has('KeyQ') ? 1 : 0) - (keys.has('KeyE') ? 1 : 0);
  const wantTilt = (keys.has('KeyR') ? 1 : 0) - (keys.has('KeyF') ? 1 : 0);
  rotVel += (wantRot * 1.2 - rotVel) * (1 - Math.exp(-dt * 8));
  tiltVel += (wantTilt * 0.8 - tiltVel) * (1 - Math.exp(-dt * 8));
  if (vel.lengthSq() > 1e-4) {
    const d = vel.clone().multiplyScalar(dt);
    camera.position.add(d); controls.target.add(d);
  }
  if (Math.abs(rotVel) > 1e-4 || Math.abs(tiltVel) > 1e-4) {
    off.copy(camera.position).sub(controls.target);
    const sph = new THREE.Spherical().setFromVector3(off);
    sph.theta += rotVel * dt;
    sph.phi = THREE.MathUtils.clamp(sph.phi - tiltVel * dt, 0.05, controls.maxPolarAngle);
    off.setFromSpherical(sph);
    camera.position.copy(controls.target).add(off);
  }
}

// dvojklik → plynulý přelet na místo
const ray = new THREE.Raycaster();
let fly = null;
renderer.domElement.addEventListener('dblclick', (e) => {
  ray.setFromCamera(new THREE.Vector2(e.clientX / innerWidth * 2 - 1, -(e.clientY / innerHeight) * 2 + 1), camera);
  const terr = world.chunks.map((c) => c.terrain).filter(Boolean);
  const hit = ray.intersectObjects(terr, false)[0];
  if (!hit) return;
  const dist = Math.min(camera.position.distanceTo(controls.target), 600) * 0.6 + 120;
  const dir = camera.position.clone().sub(controls.target).normalize();
  fly = {
    t: 0, dur: 1.4,
    t0: controls.target.clone(), t1: hit.point.clone(),
    c0: camera.position.clone(), c1: hit.point.clone().addScaledVector(dir, dist),
  };
});
function flyTo(point, dist, dur = 1.6) {
  const dir = camera.position.clone().sub(controls.target);
  dir.y = Math.max(dir.y, dir.length() * 0.55);   // pohled šikmo shora
  dir.normalize();
  fly = {
    t: 0, dur,
    t0: controls.target.clone(), t1: point.clone(),
    c0: camera.position.clone(), c1: point.clone().addScaledVector(dir, dist),
  };
}

// značka výsledku hledání
const pin = new THREE.Group();
{
  const mat = new THREE.MeshStandardMaterial({ color: 0xe3a85c, emissive: 0xe3a85c, emissiveIntensity: 0.6 });
  const cone = new THREE.Mesh(new THREE.ConeGeometry(0.5, 1.6, 16), mat);
  cone.rotation.x = Math.PI; cone.position.y = 0.8;
  const ball = new THREE.Mesh(new THREE.SphereGeometry(0.62, 20, 14), mat);
  ball.position.y = 1.9;
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.9, 1.15, 40), new THREE.MeshBasicMaterial({ color: 0xe3a85c, transparent: true, opacity: 0.8, side: THREE.DoubleSide, depthWrite: false }));
  ring.rotation.x = -Math.PI / 2; ring.position.y = 0.05;
  pin.add(cone, ball, ring);
  pin.userData.ring = ring;
  const div = document.createElement('div');
  div.className = 'lbl pin';
  pin.userData.label = new CSS2DObject(div);
  pin.add(pin.userData.label);
  pin.visible = false;
  scene.add(pin);
}
let pinT = 0;
function updatePin(dt) {
  if (!pin.visible || pinT > 4) return;   // animace jen 4 s, pak klid
  pinT += dt;
  invalidate();
  const s = THREE.MathUtils.clamp(camera.position.distanceTo(pin.position) * 0.012, 2, 40);
  pin.scale.setScalar(s);
  pin.children[0].position.y = 0.8 + Math.abs(Math.sin(pinT * 3)) * 0.4;
  pin.children[1].position.y = 1.9 + Math.abs(Math.sin(pinT * 3)) * 0.4;
  const k = (pinT % 1.5) / 1.5;
  pin.userData.ring.scale.setScalar(1 + k * 2.5);
  pin.userData.ring.material.opacity = 0.8 * (1 - k);
  pin.userData.label.position.y = 2.6;
}

const FLY_DIST = { 'adresa': 170, 'budova': 220, 'ulice': 380, 'místo zájmu': 300, 'oblast': 650, 'voda': 700, 'místo': 1300 };
initSearch((it) => {
  // výška: přesně z terénu, pokud je dlaždice načtená
  const p = new THREE.Vector3(it.x, it.y, it.z);
  const h = world.heightAt(it.x, it.z);
  if (h !== null) p.y = h;
  pin.position.copy(p);
  pin.userData.label.element.textContent = it.label;
  pin.visible = true;
  pinT = 0;
  invalidate();
  if (drive.active) { drive.place(p); return; }
  flyTo(p, FLY_DIST[it.kind] ?? 300, Math.min(3, 1.2 + controls.target.distanceTo(p) / 4000));
});

const drive = new Drive({ scene, camera, world, uniforms });
const driveBtn = document.getElementById('drive');
async function startDrive() {
  fly = null;
  driveBtn.textContent = 'Načítám…';
  await drive.start(controls.target);
  controls.enabled = false;
  driveBtn.textContent = '✕ Ukončit jízdu';
  driveBtn.classList.add('on');
  driveBtn.blur();
}
function stopDrive() {
  drive.stop();
  // orbit kamera kolem auta
  controls.target.copy(drive.pos);
  controls.enabled = true;
  controls.update();
  driveBtn.textContent = '🚗 Řídit auto';
  driveBtn.classList.remove('on');
}
driveBtn.onclick = () => (drive.active ? stopDrive() : startDrive());

const ease = (x) => (x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2);
for (const ev of ['pointerdown', 'wheel']) renderer.domElement.addEventListener(ev, () => { fly = null; });

function flyStep(dt) {
  if (!fly) return;
  fly.t = Math.min(1, fly.t + dt / fly.dur);
  const k = ease(fly.t);
  controls.target.lerpVectors(fly.t0, fly.t1, k);
  camera.position.lerpVectors(fly.c0, fly.c1, k);
  // oblouk: během přeletu kamera mírně vystoupá
  camera.position.y += Math.sin(Math.PI * k) * fly.c0.distanceTo(fly.c1) * 0.15;
  if (fly.t >= 1) fly = null;
}

// adaptivní rozlišení podle doby snímku (jen při souvislém renderu)
let maxDpr = Math.min(devicePixelRatio, 1.5);
let dpr = maxDpr, frames = 0, fpsT = 0;
function adapt(dt, cap) {
  frames++; fpsT += dt;
  if (fpsT < 1.5) return;
  const fps = frames / fpsT;
  frames = 0; fpsT = 0;
  const next = fps < cap * 0.7 ? Math.max(0.75, dpr - 0.25) : fps > cap * 0.95 ? Math.min(maxDpr, dpr + 0.25) : dpr;
  if (next !== dpr) { dpr = next; renderer.setPixelRatio(dpr); }
}

let last = performance.now(), acc = 0;
const lastShadowAt = new THREE.Vector3(1e9, 0, 0);
renderer.setAnimationLoop((now) => {
  // limit FPS (ProMotion by jinak jel na 120 Hz = dvojnásob práce)
  const cap = eco ? 30 : 60;
  if (now - last < 1000 / cap - 2) return;
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;

  const moving = drive.active || fly || vel.lengthSq() > 1e-3 || Math.abs(rotVel) > 1e-3 || Math.abs(tiltVel) > 1e-3
    || [...keys].some((k) => /^(Key[WASDQERF]|Arrow)/.test(k));
  if (moving) invalidate();
  if (drive.active) {
    drive.update(dt, keys);
    controls.target.copy(drive.pos);      // stíny, načítání a popisky sledují auto
  } else {
    keyboard(dt);
    flyStep(dt);
    controls.update();
  }

  // cíl kamery plynule drží výšku terénu
  const h = world.heightAt(controls.target.x, controls.target.z);
  if (h !== null && !fly && !drive.active) {
    const dy = (h - controls.target.y) * (1 - Math.exp(-dt * 5));
    if (Math.abs(dy) > 0.005) {
      controls.target.y += dy;
      camera.position.y += dy;
      invalidate();
    }
  }
  // kamera nesmí pod terén / do budov
  const hc = world.heightAt(camera.position.x, camera.position.z);
  if (hc !== null && !drive.active && camera.position.y < hc + 20) camera.position.y = hc + 20;

  acc += dt;
  if (acc > 0.2) {
    acc = 0;
    world.update(controls.target, vel.lengthSq() > 1 || fly ? 2 : 4, camera);
    if (dirty) updateLabels();
    status.textContent = world.pending ? `Načítám… (${world.pending})` : '';
  }
  updatePin(dt);
  if (!dirty) return;           // nic se nezměnilo → nekreslit
  dirty--;
  if (moving) adapt(dt, cap);

  // stín sleduje kameru
  const dir = sun.userData.dir;
  sun.target.position.copy(controls.target);
  sun.position.copy(controls.target).addScaledVector(dir, 3000);
  const camDist = camera.position.distanceTo(controls.target);
  const sz = THREE.MathUtils.clamp(camDist * 0.9, 300, 2500);
  if (Math.abs(sun.shadow.camera.right - sz) > 50) {
    Object.assign(sun.shadow.camera, { left: -sz, right: sz, top: sz, bottom: -sz });
    sun.shadow.camera.updateProjectionMatrix();
    shadowDirty = true;
  }
  // stínová mapa jen po posunu o kus dál, při změně scény, nebo při jízdě (auto vrhá stín)
  if (lastShadowAt.distanceTo(controls.target) > sz * 0.05) shadowDirty = true;
  if (drive.active && drive.speed !== 0) shadowDirty = true;
  if (shadowDirty && sun.castShadow) {
    renderer.shadowMap.needsUpdate = true;
    lastShadowAt.copy(controls.target);
    shadowDirty = false;
  }

  // blízká rovina podle vzdálenosti kamery → přesný depth buffer i bez log. hloubky
  const near = THREE.MathUtils.clamp(camera.position.distanceTo(controls.target) * 0.05, 0.4, 60);
  if (Math.abs(camera.near - near) > near * 0.05) { camera.near = near; camera.updateProjectionMatrix(); }

  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
});
