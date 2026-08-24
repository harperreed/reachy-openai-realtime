// ABOUTME: Classifies strict safety-sleep runtime status tuples for the dashboard.
// ABOUTME: Keeps wake-mode and always-on operator actions distinct.
(function exposeStatusClassifier(root, factory) {
  const classifier = factory();
  if (typeof module === "object" && module.exports) module.exports = classifier;
  root.ReachyStatus = classifier;
}(typeof globalThis === "object" ? globalThis : window, () => {
  function classifySafetySleep(status) {
    if (!status || typeof status !== "object") return null;
    if (status.connected !== false
      || status.wake_latched !== true
      || status.wake_latch_reason !== "noise_bail"
      || status.last_error !== null) {
      return null;
    }
    if (status.presence === "sleeping") return "wake";
    if (status.presence === null && status.phase === "safety_sleep") return "always_on";
    return null;
  }

  return { classifySafetySleep };
}));
