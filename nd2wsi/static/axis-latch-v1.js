/* Keep one dominant trackpad axis until an idle gap ends the gesture. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2AxisLatch = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const finiteNumber = (value, fallback = 0) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };

  const clockNow = () => (
    typeof performance !== "undefined" && typeof performance.now === "function"
      ? performance.now()
      : Date.now()
  );

  function normalizeWheelDelta(delta, deltaMode = 0, pagePixels = 800) {
    let scale = 1;
    if (deltaMode === 1) scale = 16;
    else if (deltaMode === 2) scale = Math.max(1, finiteNumber(pagePixels, 800));
    return finiteNumber(delta) * scale;
  }

  class AxisLatch {
    constructor({ idleMs = 100, dominance = 1.2, threshold = 3 } = {}) {
      this.idleMs = Math.max(0, finiteNumber(idleMs, 100));
      this.dominance = Math.max(1, finiteNumber(dominance, 1.2));
      this.threshold = Math.max(0, finiteNumber(threshold, 3));
      this.reset();
    }

    reset() {
      this.axis = null;
      this.sumX = 0;
      this.sumY = 0;
      this.lastAt = null;
    }

    isNewGesture(at = clockNow()) {
      const now = finiteNumber(at, clockNow());
      if (this.lastAt === null) return true;
      const gap = now - this.lastAt;
      return gap < 0 || gap > this.idleMs;
    }

    feed(deltaX, deltaY, at = clockNow()) {
      const now = finiteNumber(at, clockNow());
      if (this.isNewGesture(now)) this.reset();
      this.lastAt = now;
      // Use net displacement while deciding the axis. A real trackpad adds
      // small, alternating cross-axis deltas as the fingers wobble; summing
      // their magnitudes can make a clearly horizontal stroke look diagonal
      // forever. Once selected, the axis remains latched until idle.
      this.sumX += finiteNumber(deltaX);
      this.sumY += finiteNumber(deltaY);

      if (!this.axis) {
        const ax = Math.abs(this.sumX);
        const ay = Math.abs(this.sumY);
        if (Math.max(ax, ay) < this.threshold) return null;
        if (ax >= ay * this.dominance) this.axis = "x";
        else if (ay >= ax * this.dominance) this.axis = "y";
      }
      return this.axis;
    }
  }

  class WheelGestureSession {
    constructor({
      mode = "grid",
      idleMs = 100,
      dominance = 1.2,
      threshold = 3,
      timeStep = 60,
      timeStartStep = 18,
      zStep = 40,
    } = {}) {
      this.mode = mode === "focus" ? "focus" : "grid";
      this.timeStep = Math.max(1, finiteNumber(timeStep, 60));
      this.timeStartStep = Math.min(
        this.timeStep,
        Math.max(0.01, finiteNumber(timeStartStep, 18)),
      );
      this.zStep = Math.max(1, finiteNumber(zStep, 40));
      this.latch = new AxisLatch({ idleMs, dominance, threshold });
      this.reset();
    }

    reset() {
      this.latch.reset();
      this.accX = 0;
      this.accY = 0;
      this.timeStarted = false;
      this.focusYMode = null;
    }

    feed({
      deltaX = 0,
      deltaY = 0,
      deltaMode = 0,
      pagePixels = 800,
      altKey = false,
      at,
    } = {}) {
      const now = finiteNumber(at, clockNow());
      if (this.latch.isNewGesture(now)) {
        this.accX = 0;
        this.accY = 0;
        this.timeStarted = false;
        this.focusYMode = this.mode === "focus" ? (altKey ? "z" : "zoom") : null;
      }

      const dx = normalizeWheelDelta(deltaX, deltaMode, pagePixels);
      const dy = normalizeWheelDelta(deltaY, deltaMode, pagePixels);
      this.accX += dx;
      this.accY += dy;
      const axis = this.latch.feed(dx, dy, now);
      const result = {
        axis,
        consume: true,
        timeSteps: 0,
        zSteps: 0,
        focusYMode: this.focusYMode,
      };
      if (!axis) return result;

      if (axis === "x") {
        if (!this.timeStarted && Math.abs(this.accX) >= this.timeStartStep) {
          const direction = Math.sign(this.accX);
          this.accX -= direction * this.timeStartStep;
          this.timeStarted = true;
          result.timeSteps = direction;
          return result;
        }
        if (!this.timeStarted) return result;
        const steps = Math.trunc(this.accX / this.timeStep);
        this.accX -= steps * this.timeStep;
        result.timeSteps = steps;
        return result;
      }

      if (this.mode === "focus" && this.focusYMode === "zoom") {
        result.consume = false;
        return result;
      }

      const steps = Math.trunc(this.accY / this.zStep);
      this.accY -= steps * this.zStep;
      result.zSteps = -steps;
      return result;
    }
  }

  return { AxisLatch, WheelGestureSession, normalizeWheelDelta };
});
