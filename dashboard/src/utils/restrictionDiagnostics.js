/**
 * Passive config restriction diagnostics — /api/health & /api/startup/status.
 */

const STANDBY_MESSAGE =
  "⚠️ SYSTEM STANDBY: Sizing Restricted (POS 0/0) / Rotation Filter Engaged";

export function resolveRestrictionDiagnostics(payload) {
  if (!payload || typeof payload !== "object") {
    return {
      active: false,
      sizingRestricted: false,
      rotationFilterEngaged: false,
      maxOpenPositions: null,
      message: "",
    };
  }

  const hydration = payload.system_state?.hydration || {};
  const config = payload.config || {};
  const maxOpen =
    hydration.max_open_positions ??
    config.max_open_positions ??
    // /state has top-level max_open_positions even when /api/health enrichment is not warmed.
    payload.max_open_positions ??
    null;
  const rotationFilter =
    config.enforce_top3_rotation_filter ??
    payload.system_state?.config?.enforce_top3_rotation_filter;

  // Treat "missing" as unknown (do not lock UI into POS 0/0 by default).
  const sizingRestricted = maxOpen !== null && Number(maxOpen) === 0;
  const rotationFilterEngaged = rotationFilter === true;
  const active = sizingRestricted || rotationFilterEngaged;

  return {
    active,
    sizingRestricted,
    rotationFilterEngaged,
    maxOpenPositions: maxOpen,
    message: active ? STANDBY_MESSAGE : "",
  };
}

export function isSystemStandbyRestricted(payload) {
  return resolveRestrictionDiagnostics(payload).active;
}

export { STANDBY_MESSAGE };
