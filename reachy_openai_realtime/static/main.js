const form = document.getElementById("key-form");
const keyInput = document.getElementById("api-key");
const saveButton = document.getElementById("save-button");
const removeButton = document.getElementById("remove-button");
const message = document.getElementById("message");
const languageSelect = document.getElementById("language-select");
const i18n = window.ReachyI18n;
let firstConfigRender = true;
let refreshInFlight = false;
let refreshMemoryInFlight = false;
let cameraEnabled = false;
let lastCameraFrameAt = 0;
let selectedLanguage = "en";

function t(key, params = {}) {
  return i18n.t(selectedLanguage, key, params);
}

function applyStaticTranslations() {
  document.documentElement.lang = selectedLanguage;
  for (const element of document.querySelectorAll("[data-i18n]")) {
    element.textContent = t(element.dataset.i18n);
  }
  for (const element of document.querySelectorAll("[data-i18n-aria-label]")) {
    element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
  }
  for (const element of document.querySelectorAll("[data-i18n-alt]")) {
    element.setAttribute("alt", t(element.dataset.i18nAlt));
  }
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) {
    element.setAttribute("placeholder", t(element.dataset.i18nPlaceholder));
  }
}

function setMessage(text, error = false) {
  message.textContent = text;
  message.classList.toggle("error", error);
}

async function errorMessage(response, fallback) {
  try {
    const body = await response.json();
    // The robot API keeps a safe Japanese diagnostic for compatibility. Avoid
    // mixing it into other UI languages; the translated action-specific error
    // is clearer there.
    return selectedLanguage === "ja" && typeof body.detail === "string"
      ? body.detail
      : fallback;
  } catch (_) {
    return fallback;
  }
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString(i18n.localeFor(selectedLanguage), {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString(i18n.localeFor(selectedLanguage), {
      dateStyle: "medium", timeStyle: "short",
    });
}

function formatNumber(value) {
  return new Intl.NumberFormat(i18n.localeFor(selectedLanguage), {
    maximumFractionDigits: 0,
  }).format(Number(value || 0));
}

function formatUsd(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(i18n.localeFor(selectedLanguage), {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(value);
}

function renderUsage(usage) {
  const lifetime = usage && usage.lifetime ? usage.lifetime : {};
  document.getElementById("usage-total-tokens").textContent = formatNumber(lifetime.total_tokens);
  document.getElementById("usage-estimated-cost").textContent = formatUsd(lifetime.estimated_cost_usd);
  document.getElementById("usage-input-tokens").textContent = formatNumber(lifetime.input_tokens);
  document.getElementById("usage-output-tokens").textContent = formatNumber(lifetime.output_tokens);
  document.getElementById("usage-cached-tokens").textContent = formatNumber(lifetime.cached_input_tokens);
  document.getElementById("usage-responses").textContent = formatNumber(lifetime.responses);
  document.getElementById("usage-modalities").textContent = t("usage_modalities", {
    audioIn: formatNumber(lifetime.input_audio_tokens),
    audioOut: formatNumber(lifetime.output_audio_tokens),
    textIn: formatNumber(lifetime.input_text_tokens),
    textOut: formatNumber(lifetime.output_text_tokens),
    image: formatNumber(lifetime.input_image_tokens),
  });
  document.getElementById("usage-tracking-since").textContent = t("usage_tracking_since", {
    time: formatDateTime(usage && usage.tracking_started_at),
  });
  document.getElementById("usage-notice").textContent = t("usage_notice", {
    date: usage && usage.pricing_as_of ? usage.pricing_as_of : "—",
  });
}

function renderConfig(config) {
  const languages = Array.isArray(config.languages) ? config.languages : [];
  if (languages.length) {
    const currentOptions = Array.from(languageSelect.options).map((option) => option.value).join(",");
    const nextOptions = languages.map((language) => language.code).join(",");
    if (currentOptions !== nextOptions) {
      languageSelect.replaceChildren();
      for (const language of languages) {
        const option = document.createElement("option");
        option.value = language.code;
        option.textContent = language.label;
        languageSelect.appendChild(option);
      }
    }
  }

  selectedLanguage = config.language || "en";
  applyStaticTranslations();
  languageSelect.value = selectedLanguage;
  languageSelect.disabled = false;

  const keyState = document.getElementById("key-state");
  keyState.textContent = config.configured ? t("key_configured") : t("key_unconfigured");
  keyState.classList.toggle("ready", config.configured);
  document.getElementById("model").textContent = config.model;
  document.getElementById("voice").textContent = config.voice;
  const selectedOption = languageSelect.selectedOptions[0];
  const languageLabel = selectedOption ? selectedOption.textContent : selectedLanguage;
  document.getElementById("language-state").textContent = languageLabel;
  document.getElementById("language").textContent = languageLabel;
  document.getElementById("app-version").textContent = config.app_version || "—";

  cameraEnabled = Boolean(config.camera_enabled);
  const cameraState = document.getElementById("camera-state");
  const cameraToggle = document.getElementById("camera-toggle");
  const cameraPreview = document.getElementById("camera-preview");
  const cameraAvailable = Boolean(config.camera_available);
  cameraState.textContent = !cameraAvailable
    ? t("unavailable")
    : cameraEnabled ? t("camera_state_on") : t("off");
  cameraState.classList.toggle("ready", cameraAvailable && cameraEnabled);
  cameraToggle.disabled = !cameraAvailable;
  cameraToggle.textContent = cameraEnabled ? t("camera_off") : t("camera_on");
  cameraPreview.hidden = !cameraEnabled;
  if (cameraEnabled && Date.now() - lastCameraFrameAt > 1500) {
    cameraPreview.src = `/api/camera/snapshot?t=${Date.now()}`;
    lastCameraFrameAt = Date.now();
  }

  removeButton.hidden = !config.configured;
  if (firstConfigRender) {
    document.getElementById("settings-panel").open = !config.configured;
    firstConfigRender = false;
  }
}

languageSelect.addEventListener("change", async (event) => {
  const previousLanguage = selectedLanguage;
  const nextLanguage = event.currentTarget.value;
  event.currentTarget.disabled = true;
  selectedLanguage = nextLanguage;
  applyStaticTranslations();
  try {
    const response = await fetch("/api/config/language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: nextLanguage }),
    });
    if (!response.ok) throw new Error(await errorMessage(response, t("language_save_failed")));
    const result = await response.json();
    selectedLanguage = result.language;
    applyStaticTranslations();
    setMessage(t("language_changed", { language: result.label }));
    await refresh();
  } catch (error) {
    selectedLanguage = previousLanguage;
    event.currentTarget.value = previousLanguage;
    applyStaticTranslations();
    setMessage(error.message, true);
  } finally {
    event.currentTarget.disabled = false;
  }
});

function renderRuntime(status) {
  const knownPhase = [
    "starting", "waiting_key", "starting_audio", "tuning_audio", "connecting",
    "reconnecting", "listening", "user_speaking", "thinking", "responding",
    "assistant_speaking", "disconnected", "error", "stopped",
  ].includes(status.phase) ? status.phase : "starting";
  document.getElementById("status-dot").className = `status-dot ${knownPhase}`;
  document.getElementById("status-title").textContent = t(`phase_${knownPhase}`);
  document.getElementById("status-detail").textContent = status.detail
    ? i18n.translateRuntime(
      selectedLanguage,
      status.detail,
      status.detail_key,
      status.detail_params,
      knownPhase,
    )
    : t("no_status_detail");

  const connection = document.getElementById("connection-state");
  connection.textContent = status.connected ? t("api_connected") : t("api_disconnected");
  connection.classList.toggle("ready", status.connected);

  const errorPanel = document.getElementById("error-panel");
  errorPanel.hidden = !status.last_error;
  document.getElementById("runtime-error").textContent = status.last_error || "";
  document.getElementById("last-user").textContent = status.last_user
    || (status.audio_commits > 0 ? t("audio_sent_no_transcript") : t("not_spoken"));
  document.getElementById("last-assistant").textContent = status.last_assistant || t("waiting_response");
  document.getElementById("last-motion").textContent = status.last_motion || "—";
  document.getElementById("updated-at").textContent = formatTime(status.updated_at);
  renderUsage(status.usage);

  const micLevel = typeof status.mic_dbfs === "number" ? status.mic_dbfs : -80;
  document.getElementById("mic-meter").value = micLevel;
  document.getElementById("mic-level").textContent = status.mic_dbfs == null
    ? t("loading")
    : `${micLevel.toFixed(1)} dBFS`;

  const cameraImagesSent = Number(status.camera_images_sent || 0);
  const cameraSuffix = status.last_camera_image_at
    ? t("camera_send_last", { time: formatTime(status.last_camera_image_at) })
    : t("camera_send_next");
  document.getElementById("camera-send-status").textContent = t("camera_send_status", {
    count: cameraImagesSent,
    suffix: cameraSuffix,
  });

  const presenceStates = ["booting", "sleeping", "waking", "awake", "error"];
  const presence = presenceStates.includes(status.presence) ? status.presence : null;
  const wakeState = document.getElementById("wake-state");
  wakeState.textContent = presence ? t(`presence_${presence}`) : t("wake_disabled");
  wakeState.classList.toggle("ready", presence === "awake");
  // Enable each control only from a state its endpoint accepts: request_wake
  // takes SLEEPING or ERROR, request_sleep takes AWAKE (Task 14/§24).
  document.getElementById("wake-button").disabled = !(presence === "sleeping" || presence === "error");
  document.getElementById("sleep-button").disabled = presence !== "awake";

  const events = Array.isArray(status.events) ? status.events : [];
  document.getElementById("event-count").textContent = String(events.length);
  const list = document.getElementById("event-list");
  list.replaceChildren();
  for (const item of events) {
    const row = document.createElement("li");
    row.className = item.level || "info";
    const time = document.createElement("time");
    time.dateTime = item.time || "";
    time.textContent = formatTime(item.time);
    const text = document.createElement("span");
    text.textContent = i18n.translateRuntime(
      selectedLanguage,
      item.message || "",
      item.key,
      item.params,
      knownPhase,
    );
    row.append(time, text);
    list.appendChild(row);
  }
  if (!events.length) {
    const row = document.createElement("li");
    row.textContent = t("no_logs");
    list.appendChild(row);
  }
}

async function refreshMemory() {
  if (refreshMemoryInFlight) return;
  refreshMemoryInFlight = true;
  const list = document.getElementById("memory-list");
  const empty = document.getElementById("memory-empty");
  const unavailable = document.getElementById("memory-unavailable");
  const count = document.getElementById("memory-count");
  try {
    const query = document.getElementById("memory-search").value.trim();
    const url = query ? `/api/memory?q=${encodeURIComponent(query)}` : "/api/memory";
    const response = await fetch(url);
    const data = await response.json();
    list.replaceChildren();
    unavailable.hidden = data.ok !== false;
    if (data.ok === false) {
      count.textContent = "";
      return;
    }
    count.textContent = String(data.count);
    empty.hidden = data.memories.length > 0;
    for (const memory of data.memories) {
      list.appendChild(renderMemoryItem(memory));
    }
  } catch (_) {
    list.replaceChildren();
    count.textContent = "";
    unavailable.hidden = false;
  } finally {
    refreshMemoryInFlight = false;
  }
}

function renderMemoryItem(memory) {
  const item = document.createElement("li");
  item.className = "memory-item";
  const text = document.createElement("span");
  text.className = "memory-text";
  text.textContent = memory.text;
  const meta = document.createElement("span");
  meta.className = "memory-meta";
  meta.textContent = `${memory.kind} · ${memory.source}`;
  const pin = document.createElement("button");
  pin.textContent = memory.pinned ? "★" : "☆";
  pin.addEventListener("click", async () => {
    await fetch(`/api/memory/${encodeURIComponent(memory.id)}/pin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned: !memory.pinned }),
    });
    await refreshMemory();
  });
  const remove = document.createElement("button");
  remove.textContent = "✕";
  remove.addEventListener("click", async () => {
    await fetch(`/api/memory/${encodeURIComponent(memory.id)}`, { method: "DELETE" });
    await refreshMemory();
  });
  item.append(text, meta, pin, remove);
  return item;
}

document.getElementById("memory-search").addEventListener("input", () => {
  clearTimeout(refreshMemory._debounce);
  refreshMemory._debounce = setTimeout(refreshMemory, 300);
});

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const [configResponse, statusResponse] = await Promise.all([
      fetch("/api/config", { cache: "no-store" }),
      fetch("/api/status", { cache: "no-store" }),
    ]);
    if (!configResponse.ok || !statusResponse.ok) throw new Error(t("robot_status_failed"));
    renderConfig(await configResponse.json());
    renderRuntime(await statusResponse.json());
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    refreshInFlight = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveButton.disabled = true;
  setMessage(t("saving"));
  try {
    const response = await fetch("/api/config/api-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: keyInput.value }),
    });
    if (!response.ok) throw new Error(await errorMessage(response, t("key_save_failed")));
    const result = await response.json();
    keyInput.value = "";
    setMessage(result.restart_required ? t("saved_restart") : t("saved_start"));
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

removeButton.addEventListener("click", async () => {
  if (!confirm(t("confirm_remove"))) return;
  try {
    const response = await fetch("/api/config/api-key", { method: "DELETE" });
    if (!response.ok) throw new Error(await errorMessage(response, t("key_remove_failed")));
    const result = await response.json();
    setMessage(result.restart_required ? t("removed_restart") : t("removed"));
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.getElementById("toggle-key").addEventListener("click", (event) => {
  const hidden = keyInput.type === "password";
  keyInput.type = hidden ? "text" : "password";
  event.currentTarget.textContent = hidden ? t("hide") : t("show");
});

document.getElementById("camera-toggle").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    const response = await fetch("/api/config/camera", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !cameraEnabled }),
    });
    if (!response.ok) throw new Error(await errorMessage(response, t("camera_setting_failed")));
    const result = await response.json();
    cameraEnabled = Boolean(result.camera_enabled);
    setMessage(t(cameraEnabled ? "camera_enabled_message" : "camera_disabled_message"));
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    event.currentTarget.disabled = false;
  }
});

document.getElementById("wake-button").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    const response = await fetch("/api/presence/wake", { method: "POST" });
    if (!response.ok) throw new Error(await errorMessage(response, t("wake_failed")));
    const result = await response.json();
    if (result.ok === false) throw new Error(t("wake_failed"));
    setMessage(t("wake_requested"));
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.getElementById("sleep-button").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    const response = await fetch("/api/presence/sleep", { method: "POST" });
    if (!response.ok) throw new Error(await errorMessage(response, t("sleep_failed")));
    const result = await response.json();
    if (result.ok === false) throw new Error(t("sleep_failed"));
    setMessage(t("sleep_requested"));
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.getElementById("copy-diagnostics").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/diagnostics", { cache: "no-store" });
    if (!response.ok) throw new Error(t("diagnostics_failed"));
    const text = JSON.stringify(await response.json(), null, 2);
    await navigator.clipboard.writeText(text);
    setMessage(t("diagnostics_copied"));
  } catch (error) {
    setMessage(error.message, true);
  }
});

applyStaticTranslations();
refresh();
refreshMemory();
window.setInterval(refresh, 1000);
window.setInterval(refreshMemory, 5000);
