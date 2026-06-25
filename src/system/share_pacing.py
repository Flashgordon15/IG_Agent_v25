"""
SHARE feedback pacing — RTT-driven worker + interval scaling for saturation harness.

When ``IG_SHARE_ENGINE=1``, torture / soak drivers use this PID loop to widen
pacing under broker pressure and add workers when RTT is healthy.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SharePacingProfile:
    workers: int = 4
    pacing_ms: float = 200.0
    rtt_ms: float = 0.0
    shifts: list[dict[str, float | int]] = field(default_factory=list)


class SharePacingController:
  """Adaptive pacing for concurrent broker REST saturation."""

  def __init__(
      self,
      *,
      min_workers: int = 4,
      max_workers: int = 16,
      min_pacing_ms: float = 200.0,
      max_pacing_ms: float = 2000.0,
      target_rtt_ms: float = 350.0,
  ) -> None:
      self._min_workers = int(min_workers)
      self._max_workers = int(max_workers)
      self._min_pacing_ms = float(min_pacing_ms)
      self._max_pacing_ms = float(max_pacing_ms)
      self._target_rtt_ms = float(target_rtt_ms)
      self._lock = threading.Lock()
      self.profile = SharePacingProfile(workers=self._min_workers, pacing_ms=self._min_pacing_ms)

  def enabled(self) -> bool:
      return os.environ.get("IG_SHARE_ENGINE", "").strip().lower() in ("1", "true", "yes")

  def observe_rtt(self, rtt_ms: float) -> SharePacingProfile:
      """Feed round-trip latency; returns updated profile."""
      if not self.enabled():
          return self.profile
      rtt = max(0.0, float(rtt_ms))
      with self._lock:
          prev_workers = self.profile.workers
          prev_pacing = self.profile.pacing_ms
          err = rtt - self._target_rtt_ms
          # High RTT → fewer workers, slower pacing (up to 2000ms).
          if err > 150.0:
              workers = max(self._min_workers, self.profile.workers - 1)
              pacing = min(self._max_pacing_ms, self.profile.pacing_ms + 120.0)
          elif err < -80.0:
              workers = min(self._max_workers, self.profile.workers + 1)
              pacing = max(self._min_pacing_ms, self.profile.pacing_ms - 80.0)
          else:
              workers = self.profile.workers
              pacing = self.profile.pacing_ms
          if workers != prev_workers or abs(pacing - prev_pacing) > 1.0:
              self.profile.shifts.append(
                  {
                      "ts": time.time(),
                      "rtt_ms": round(rtt, 2),
                      "workers": workers,
                      "pacing_ms": round(pacing, 2),
                  }
              )
          self.profile.workers = workers
          self.profile.pacing_ms = pacing
          self.profile.rtt_ms = rtt
          return SharePacingProfile(
              workers=workers,
              pacing_ms=pacing,
              rtt_ms=rtt,
              shifts=list(self.profile.shifts),
          )

  def pacing_interval_sec(self) -> float:
      return max(self._min_pacing_ms, self.profile.pacing_ms) / 1000.0
