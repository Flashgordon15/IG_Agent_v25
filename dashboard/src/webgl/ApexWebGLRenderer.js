import * as THREE from "three";
import {
  EXEC_CONFIDENCE_FLOOR,
  EXEC_FITNESS_FLOOR_PCT,
  PRIMARY_RING_KEYS,
} from "../apex/constants.js";
import { PriceHistoryRing, TickInterpolator } from "./TickInterpolator.js";

const HISTORY_CAP = 120;
const RING_RADIUS = 1.35;
const RING_TUBE_BASE = 0.058;
const THRESHOLD_RING_SCALE = 1.22;
const THRESHOLD_COLOR = 0xff0055;

/** Neon gradient stops — Gold: orange→magenta, Wall St: cyan→violet */
const RING_GRADIENTS = {
  GOLD: { a: 0xff9f1c, b: 0xe63946 },
  WALL_STREET: { a: 0x00b4d8, b: 0x7209b7 },
};

/**
 * Apply vertex-color gradient along torus tube for neon mesh glow.
 * @param {THREE.BufferGeometry} geo
 * @param {number} colorA
 * @param {number} colorB
 */
function applyTorusGradient(geo, colorA, colorB) {
  const pos = geo.attributes.position;
  const ca = new THREE.Color(colorA);
  const cb = new THREE.Color(colorB);
  const tmp = new THREE.Color();
  const colors = new Float32Array(pos.count * 3);
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i);
    const y = pos.getY(i);
    const z = pos.getZ(i);
    const angle = Math.atan2(y, x);
    const t = (Math.sin(angle * 2) + 1) * 0.5;
    tmp.copy(ca).lerp(cb, t * 0.65 + (z + RING_TUBE_BASE) * 0.35);
    colors[i * 3] = tmp.r;
    colors[i * 3 + 1] = tmp.g;
    colors[i * 3 + 2] = tmp.b;
  }
  geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
}

function createNeonTorusMaterial(colorA, colorB) {
  return new THREE.MeshStandardMaterial({
    vertexColors: true,
    emissive: new THREE.Color(colorA),
    emissiveIntensity: 0.95,
    metalness: 0.28,
    roughness: 0.12,
    transparent: true,
    opacity: 0.96,
  });
}

/**
 * GPU avionics — dual neon torus rings (Gold / Wall St) with volatility pulse.
 */
export class ApexWebGLRenderer {
  /**
   * @param {HTMLCanvasElement} canvas
   */
  constructor(canvas) {
    this.canvas = canvas;
    this.interpolator = new TickInterpolator(280);
    this.history = new PriceHistoryRing(HISTORY_CAP);

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x080b18, 1);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
    this.camera.position.set(0, 0.15, 5.8);
    this.camera.lookAt(0, 0, 0);

    const ambient = new THREE.AmbientLight(0x1a2148, 0.5);
    this.scene.add(ambient);
    const goldLight = new THREE.PointLight(0xff9f1c, 2.6, 16);
    goldLight.position.set(-3.2, 1.5, 2);
    this.scene.add(goldLight);
    const wallLight = new THREE.PointLight(0x00b4d8, 2.6, 16);
    wallLight.position.set(3.2, 1.5, 2);
    this.scene.add(wallLight);
    const amethystFill = new THREE.PointLight(0x7209b7, 0.6, 20);
    amethystFill.position.set(0, -1.5, 3);
    this.scene.add(amethystFill);

    /** @type {Map<string, THREE.Mesh>} */
    this.rings = new Map();
    /** @type {Map<string, THREE.LineLoop>} */
    this.thresholdWalls = new Map();
    /** @type {Map<string, THREE.Line>} */
    this.waves = new Map();
    /** @type {Map<string, THREE.Mesh>} */
    this.tickDots = new Map();

    this._confidence = {};
    this._fitness = {};
    this._volatility = {};

    this._buildPrimaryRings();
    this._animId = 0;
    this._running = false;
  }

  _positionForKey(key) {
    if (key === "GOLD") return [-2.75, 0, 0];
    if (key === "WALL_STREET") return [2.75, 0, 0];
    return [0, 0, 0];
  }

  _buildPrimaryRings() {
    for (const key of PRIMARY_RING_KEYS) {
      const grad = RING_GRADIENTS[key] ?? { a: 0x64748b, b: 0x334155 };
      const pos = this._positionForKey(key);

      const thresholdGeo = new THREE.TorusGeometry(
        RING_RADIUS * THRESHOLD_RING_SCALE,
        0.012,
        8,
        96,
      );
      const thresholdEdges = new THREE.EdgesGeometry(thresholdGeo);
      const thresholdMat = new THREE.LineBasicMaterial({
        color: THRESHOLD_COLOR,
        transparent: true,
        opacity: 0.95,
      });
      const threshold = new THREE.LineLoop(thresholdEdges, thresholdMat);
      threshold.position.set(...pos);
      threshold.userData.assetKey = key;
      this.scene.add(threshold);
      this.thresholdWalls.set(key, threshold);
      thresholdGeo.dispose();

      const geo = new THREE.TorusGeometry(RING_RADIUS, RING_TUBE_BASE, 24, 128);
      applyTorusGradient(geo, grad.a, grad.b);
      const mat = createNeonTorusMaterial(grad.a, grad.b);
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(...pos);
      mesh.userData.assetKey = key;
      mesh.userData.gradA = grad.a;
      mesh.userData.gradB = grad.b;
      this.scene.add(mesh);
      this.rings.set(key, mesh);

      const dotGeo = new THREE.SphereGeometry(0.072, 16, 16);
      const dotMat = new THREE.MeshStandardMaterial({
        color: grad.a,
        emissive: grad.b,
        emissiveIntensity: 1.6,
      });
      const dot = new THREE.Mesh(dotGeo, dotMat);
      dot.position.set(pos[0], pos[1], 0.22);
      this.scene.add(dot);
      this.tickDots.set(key, dot);

      const waveMat = new THREE.LineBasicMaterial({
        color: grad.a,
        transparent: true,
        opacity: 0.85,
      });
      const wavePositions = new Float32Array(HISTORY_CAP * 3);
      const waveGeo = new THREE.BufferGeometry();
      waveGeo.setAttribute(
        "position",
        new THREE.BufferAttribute(wavePositions, 3),
      );
      waveGeo.setDrawRange(0, 0);
      const wave = new THREE.Line(waveGeo, waveMat);
      wave.visible = false;
      wave.position.set(pos[0] - 1.0, pos[1] - 0.65, 0.08);
      this.scene.add(wave);
      this.waves.set(key, wave);
    }
  }

  resize(width, height) {
    if (width <= 0 || height <= 0) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  /**
   * @param {Record<string, import('../apex/types.js').AvionicsAssetTelemetry>} assets
   */
  ingestTelemetry(assets) {
    const now = performance.now();
    for (const key of PRIMARY_RING_KEYS) {
      const row = assets[key];
      const mid = row?.mid;
      if (mid == null || !Number.isFinite(Number(mid)) || !(Number(mid) > 0)) {
        continue;
      }
      const midN = Number(mid);
      this.interpolator.push(key, midN, now);
      this.history.push(key, midN);
      if (row.confidence != null) this._confidence[key] = row.confidence;
      if (row.fitness != null) this._fitness[key] = row.fitness;
      if (row.volatility != null) this._volatility[key] = row.volatility;
    }
  }

  _volatilityNorm(key) {
    const hist = this.history.get(key);
    if (hist.length < 3) return 0.08;
    const min = Math.min(...hist);
    const max = Math.max(...hist);
    const mean = hist.reduce((a, b) => a + b, 0) / hist.length;
    const span = max - min;
    if (mean <= 0) return 0.08;
    return Math.min(1, Math.max(0.04, span / mean));
  }

  _approachRatio(key) {
    const conf = this._confidence[key];
    const fit = this._fitness[key];
    const confRatio =
      conf != null ? Math.min(1.12, Math.max(0.06, conf / EXEC_CONFIDENCE_FLOOR)) : 0.08;
    let fitRatio = 0.08;
    if (fit != null) {
      const fitPct = fit <= 1 ? fit * 100 : fit;
      fitRatio = Math.min(1.12, Math.max(0.06, fitPct / EXEC_FITNESS_FLOOR_PCT));
    }
    return Math.min(1.08, Math.max(confRatio, fitRatio));
  }

  _updateRing(key, mesh, now) {
    const t = now * 0.001;
    const approach = this._approachRatio(key);
    const vol = this._volatility[key] ?? this._volatilityNorm(key);
    const tickPulse = this.interpolator.get(key) != null ? 0.06 : 0;
    const pulse =
      0.06 +
      Math.sin(t * 6.2 + (key === "GOLD" ? 0 : 1.4)) * 0.045 +
      tickPulse;
    const tube = RING_TUBE_BASE * (0.85 + vol * 5.5 + pulse * 2.2);
    const scale = 0.68 + approach * 0.42 + vol * 0.18;

    mesh.scale.set(scale, scale, scale);
    mesh.rotation.x = Math.sin(t * 0.85 + key.length) * 0.14 * vol;
    mesh.rotation.y = t * 0.28 + vol * 0.55;
    mesh.rotation.z = t * 0.22 + Math.sin(t * 4) * 0.08;

    const prevTube = mesh.userData.lastTube ?? RING_TUBE_BASE;
    if (Math.abs(prevTube - tube) > 0.0015) {
      mesh.geometry.dispose();
      const geo = new THREE.TorusGeometry(RING_RADIUS, tube, 24, 128);
      applyTorusGradient(geo, mesh.userData.gradA, mesh.userData.gradB);
      mesh.geometry = geo;
      mesh.userData.lastTube = tube;
    }

    const mat = mesh.material;
    if (mat instanceof THREE.MeshStandardMaterial) {
      const wallProximity = Math.min(1, approach);
      mat.emissiveIntensity = 0.55 + wallProximity * 1.05 + vol * 0.65 + pulse * 1.2;
      mat.opacity = 0.72 + wallProximity * 0.28;
    }

    const threshold = this.thresholdWalls.get(key);
    if (threshold) {
      threshold.rotation.z = -t * 0.06;
      threshold.scale.setScalar(0.98 + vol * 0.12);
      const matT = threshold.material;
      if (matT instanceof THREE.LineBasicMaterial) {
        matT.opacity = 0.5 + Math.sin(t * 3.5) * 0.2 + approach * 0.35;
      }
    }

    const dot = this.tickDots.get(key);
    const mid = this.interpolator.get(key);
    if (dot && mid != null) {
      const dotScale = 0.9 + vol * 3.2 + pulse * 4.5;
      dot.scale.setScalar(dotScale);
      dot.position.z = 0.18 + vol * 0.35 + Math.sin(t * 8) * 0.06;
    }
  }

  _updateWave(key, line) {
    const hist = this.history.get(key);
    if (hist.length < 2) {
      line.visible = false;
      return;
    }
    line.visible = true;
    const min = Math.min(...hist);
    const max = Math.max(...hist);
    const span = max - min || Math.max(min * 0.0001, 1);
    const attr = line.geometry.getAttribute("position");
    const arr = attr.array;
    const len = hist.length;
    const divisor = len > 1 ? len - 1 : 1;
    for (let i = 0; i < len; i++) {
      arr[i * 3] = (i / divisor) * 2.0 - 1.0;
      arr[i * 3 + 1] = ((hist[i] - min) / span) * 0.55 + 0.04;
      arr[i * 3 + 2] = 0;
    }
    attr.needsUpdate = true;
    line.geometry.setDrawRange(0, len);
  }

  _tickFrame(now) {
    for (const [key, mesh] of this.rings) {
      this._updateRing(key, mesh, now);
      const wave = this.waves.get(key);
      if (wave) this._updateWave(key, wave);
    }
    this.scene.rotation.y = Math.sin(now * 0.00022) * 0.04;
    this.renderer.render(this.scene, this.camera);
  }

  start() {
    if (this._running) return;
    this._running = true;
    const loop = (now) => {
      if (!this._running) return;
      this._animId = requestAnimationFrame(loop);
      this._tickFrame(now);
    };
    this._animId = requestAnimationFrame(loop);
  }

  stop() {
    this._running = false;
    if (this._animId) cancelAnimationFrame(this._animId);
  }

  dispose() {
    this.stop();
    for (const mesh of this.rings.values()) {
      mesh.geometry.dispose();
      mesh.material.dispose();
    }
    for (const line of this.thresholdWalls.values()) {
      line.geometry.dispose();
      line.material.dispose();
    }
    for (const line of this.waves.values()) {
      line.geometry.dispose();
      line.material.dispose();
    }
    for (const dot of this.tickDots.values()) {
      dot.geometry.dispose();
      dot.material.dispose();
    }
    this.renderer.dispose();
  }
}
