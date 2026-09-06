const $ = (id) => document.getElementById(id);
const startBtn = $("startBtn");
const stopBtn = $("stopBtn");
const autoStop = $("autoStop");
const toast = $("toast");
let lastWorkerLog = "";
let lastControllerLog = "";

function showToast(text) {
  toast.textContent = text;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3000);
}

function formatRuntime(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  seconds = Math.max(0, Number(seconds) || 0);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`;
}

function phaseClass(phase) {
  if (phase === "online") return "online";
  if (["error", "heartbeat_lost"].includes(phase)) return "error";
  if (["offline"].includes(phase)) return "offline";
  return "starting";
}

function render(state) {
  const phase = state.phase || "offline";
  $("phaseLabel").textContent = phase.replaceAll("_", " ").toUpperCase();
  $("message").textContent = state.message || "";
  $("kaggleStatus").textContent = state.kaggle_status || "—";
  $("workerStage").textContent = state.worker?.stage || "—";
  $("kernelRef").textContent = state.kernel_ref || "Not configured";
  $("runtime").textContent = formatRuntime(state.worker?.runtime_seconds);
  $("heartbeat").textContent = state.heartbeat_age_seconds === null ? "—" : `${Math.round(state.heartbeat_age_seconds)}s ago`;

  const names = state.worker?.gpu_names || [];
  const count = state.worker?.gpu_count;
  $("gpuInfo").textContent = count ? `${count} × ${names.join(", ") || "GPU"}` : "—";

  const dot = $("statusDot");
  dot.className = `dot ${phaseClass(phase)}`;

  const busy = ["starting", "submitting", "waiting_worker", "worker_starting", "online", "stopping"].includes(phase);
  startBtn.disabled = busy && phase !== "heartbeat_lost";
  stopBtn.disabled = phase === "offline";

  if (state.auto_stop_remaining_seconds !== null) {
    $("autoStopText").textContent = `Auto-stop in ${formatRuntime(state.auto_stop_remaining_seconds)}`;
  } else {
    $("autoStopText").textContent = "No active timer";
  }

  const workerLog = state.worker_log || "Waiting for worker...";
  const controllerLog = (state.controller_log || []).join("\n") || "Ready.";
  if (workerLog !== lastWorkerLog) {
    $("workerLog").textContent = workerLog;
    $("workerLog").scrollTop = $("workerLog").scrollHeight;
    lastWorkerLog = workerLog;
  }
  if (controllerLog !== lastControllerLog) {
    $("controllerLog").textContent = controllerLog;
    $("controllerLog").scrollTop = $("controllerLog").scrollHeight;
    lastControllerLog = controllerLog;
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (res.status === 401) {
      location.href = "/login";
      return;
    }
    const data = await res.json();
    if (data.ok) render(data.state);
  } catch (err) {
    $("message").textContent = "Controller connection lost. Retrying...";
  }
}

async function post(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

startBtn.addEventListener("click", async () => {
  try {
    startBtn.disabled = true;
    const minutes = Number(autoStop.value || 0);
    const data = await post("/api/start", { auto_stop_minutes: minutes });
    showToast(data.message || "Start requested");
    await refresh();
  } catch (err) {
    showToast(err.message);
    await refresh();
  }
});

stopBtn.addEventListener("click", async () => {
  if (!confirm("Stop the Telegram bot and end the Kaggle worker run?")) return;
  try {
    stopBtn.disabled = true;
    const data = await post("/api/stop");
    showToast(data.message || "Stop requested");
    await refresh();
  } catch (err) {
    showToast(err.message);
    await refresh();
  }
});

refresh();
setInterval(refresh, 3000);
