/**
 * Smooth price interpolation — lerps display mid toward live packet targets.
 */
export class TickInterpolator {
  constructor(lerpMs = 320) {
    this.lerpMs = lerpMs;
    /** @type {Map<string, { display: number, target: number, lastSet: number }>} */
    this.channels = new Map();
  }

  /**
   * @param {string} key
   * @param {number | null | undefined} target
   * @param {number} now
   */
  push(key, target, now = performance.now()) {
    if (target == null || !Number.isFinite(target)) return;
    const existing = this.channels.get(key);
    if (!existing) {
      this.channels.set(key, { display: target, target, lastSet: now });
      return;
    }
    existing.target = target;
    existing.lastSet = now;
  }

  /**
   * @param {number} now
   * @returns {Map<string, number>}
   */
  sample(now = performance.now()) {
    const out = new Map();
    for (const [key, ch] of this.channels) {
      const dt = Math.max(0, now - ch.lastSet);
      const t = Math.min(1, dt / this.lerpMs);
      const eased = t * t * (3 - 2 * t);
      ch.display = ch.display + (ch.target - ch.display) * eased;
      out.set(key, ch.display);
    }
    return out;
  }

  get(key) {
    return this.channels.get(key)?.display ?? null;
  }
}

/**
 * Ring-buffer price history for sparkline / wave transforms.
 */
export class PriceHistoryRing {
  constructor(capacity = 120) {
    this.capacity = capacity;
    /** @type {Map<string, number[]>} */
    this.buffers = new Map();
  }

  /**
   * @param {string} key
   * @param {number} value
   */
  push(key, value) {
    if (!Number.isFinite(value)) return;
    let buf = this.buffers.get(key);
    if (!buf) {
      buf = [];
      this.buffers.set(key, buf);
    }
    buf.push(value);
    if (buf.length > this.capacity) {
      buf.splice(0, buf.length - this.capacity);
    }
  }

  /**
   * @param {string} key
   * @returns {number[]}
   */
  get(key) {
    return this.buffers.get(key) ?? [];
  }
}
