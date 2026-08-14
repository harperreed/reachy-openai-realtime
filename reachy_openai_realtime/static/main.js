const form = document.getElementById("key-form");
const keyInput = document.getElementById("api-key");
const saveButton = document.getElementById("save-button");
const removeButton = document.getElementById("remove-button");
const message = document.getElementById("message");
const languageSelect = document.getElementById("language-select");
const phaseLabels = {
  starting: "起動中",
  waiting_key: "APIキー待ち",
  starting_audio: "音声準備中",
  tuning_audio: "マイク調整中",
  connecting: "API接続中",
  reconnecting: "再接続中",
  listening: "話しかけてOK",
  user_speaking: "聞き取り中",
  thinking: "理解中",
  responding: "応答中",
  assistant_speaking: "発話中",
  disconnected: "接続切れ",
  error: "エラー",
  stopped: "停止中",
};
let firstConfigRender = true;
let refreshInFlight = false;
let cameraEnabled = false;
let lastCameraFrameAt = 0;
let selectedLanguage = "en";

function setMessage(text, error = false) {
  message.textContent = text;
  message.classList.toggle("error", error);
}

async function errorMessage(response, fallback) {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : fallback;
  } catch (_) {
    return fallback;
  }
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderConfig(config) {
  const keyState = document.getElementById("key-state");
  keyState.textContent = config.configured ? "✓ APIキー設定済み" : "○ APIキー未設定";
  keyState.classList.toggle("ready", config.configured);
  document.getElementById("model").textContent = config.model;
  document.getElementById("voice").textContent = config.voice;
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
  languageSelect.value = selectedLanguage;
  languageSelect.disabled = false;
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
  cameraState.textContent = !cameraAvailable ? "利用不可" : cameraEnabled ? "ON・発話時送信" : "OFF";
  cameraState.classList.toggle("ready", cameraAvailable && cameraEnabled);
  cameraToggle.disabled = !cameraAvailable;
  cameraToggle.textContent = cameraEnabled ? "カメラをOFF" : "カメラをON";
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
  const nextLanguage = event.currentTarget.value;
  event.currentTarget.disabled = true;
  try {
    const response = await fetch("/api/config/language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: nextLanguage }),
    });
    if (!response.ok) throw new Error(await errorMessage(response, "言語設定を保存できませんでした"));
    const result = await response.json();
    selectedLanguage = result.language;
    setMessage(`${result.label}に変更しました。次の応答から反映されます。`);
    await refresh();
  } catch (error) {
    event.currentTarget.value = selectedLanguage;
    setMessage(error.message, true);
  } finally {
    event.currentTarget.disabled = false;
  }
});

function renderRuntime(status) {
  const phase = phaseLabels[status.phase] ? status.phase : "starting";
  document.getElementById("status-dot").className = `status-dot ${phase}`;
  document.getElementById("status-title").textContent = phaseLabels[status.phase] || status.phase;
  document.getElementById("status-detail").textContent = status.detail || "状態詳細はありません";

  const connection = document.getElementById("connection-state");
  connection.textContent = status.connected ? "● Realtime API接続済み" : "○ API未接続";
  connection.classList.toggle("ready", status.connected);

  const errorPanel = document.getElementById("error-panel");
  errorPanel.hidden = !status.last_error;
  document.getElementById("runtime-error").textContent = status.last_error || "";
  document.getElementById("last-user").textContent = status.last_user
    || (status.audio_commits > 0 ? "音声入力を送信しました（文字起こしなし）" : "まだ話しかけていません");
  document.getElementById("last-assistant").textContent = status.last_assistant || "応答待ちです";
  document.getElementById("last-motion").textContent = status.last_motion || "—";
  document.getElementById("updated-at").textContent = formatTime(status.updated_at);
  const micLevel = typeof status.mic_dbfs === "number" ? status.mic_dbfs : -80;
  document.getElementById("mic-meter").value = micLevel;
  document.getElementById("mic-level").textContent = status.mic_dbfs == null
    ? "確認中…"
    : `${micLevel.toFixed(1)} dBFS`;
  const cameraImagesSent = Number(status.camera_images_sent || 0);
  const lastCameraImageAt = status.last_camera_image_at
    ? `・最終 ${formatTime(status.last_camera_image_at)}`
    : "・次の発話開始時に送信";
  document.getElementById("camera-send-status").textContent =
    `OpenAI送信: ${cameraImagesSent}枚${lastCameraImageAt}`;

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
    text.textContent = item.message || "";
    row.append(time, text);
    list.appendChild(row);
  }
  if (!events.length) {
    const row = document.createElement("li");
    row.textContent = "ログはまだありません";
    list.appendChild(row);
  }
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const [configResponse, statusResponse] = await Promise.all([
      fetch("/api/config", { cache: "no-store" }),
      fetch("/api/status", { cache: "no-store" }),
    ]);
    if (!configResponse.ok || !statusResponse.ok) throw new Error("ロボットの状態を取得できませんでした");
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
  setMessage("保存中…");
  try {
    const response = await fetch("/api/config/api-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: keyInput.value }),
    });
    if (!response.ok) {
      throw new Error(await errorMessage(response, "APIキーを保存できませんでした"));
    }
    const result = await response.json();
    keyInput.value = "";
    setMessage(result.restart_required ? "保存しました。アプリを再起動してください。" : "保存しました。会話を開始します。" );
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

removeButton.addEventListener("click", async () => {
  if (!confirm("保存済みのOpenAI APIキーを削除しますか？")) return;
  try {
    const response = await fetch("/api/config/api-key", { method: "DELETE" });
    if (!response.ok) {
      throw new Error(await errorMessage(response, "APIキーを削除できませんでした"));
    }
    const result = await response.json();
    setMessage(result.restart_required ? "削除しました。アプリを再起動してください。" : "削除しました。" );
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.getElementById("toggle-key").addEventListener("click", (event) => {
  const hidden = keyInput.type === "password";
  keyInput.type = hidden ? "text" : "password";
  event.currentTarget.textContent = hidden ? "隠す" : "表示";
});

document.getElementById("camera-toggle").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    const response = await fetch("/api/config/camera", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !cameraEnabled }),
    });
    if (!response.ok) throw new Error(await errorMessage(response, "カメラ設定を変更できませんでした"));
    const result = await response.json();
    cameraEnabled = Boolean(result.camera_enabled);
    setMessage(cameraEnabled ? "AIカメラをONにしました。次の発話開始時に静止画をOpenAIへ送信します。" : "AIカメラをOFFにしました。");
    await refresh();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    event.currentTarget.disabled = false;
  }
});

document.getElementById("copy-diagnostics").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/diagnostics", { cache: "no-store" });
    if (!response.ok) throw new Error("診断ログを取得できませんでした");
    const text = JSON.stringify(await response.json(), null, 2);
    await navigator.clipboard.writeText(text);
    setMessage("診断ログをコピーしました。");
  } catch (error) {
    setMessage(error.message, true);
  }
});

refresh();
window.setInterval(refresh, 1000);
