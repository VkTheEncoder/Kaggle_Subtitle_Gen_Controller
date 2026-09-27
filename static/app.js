const $ = (id) => document.getElementById(id);
const BOT_IDS = ["subtitle", "encoding"];
const toast = $("toast");
const lastWorkerLog = {};
const lastControllerLog = {};

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
  if (phase === "offline") return "offline";
  return "starting";
}

function renderBot(botId, state) {
  const phase = state?.phase || "offline";
  $(`${botId}-phaseLabel`).textContent = phase.replaceAll("_", " ").toUpperCase();
  $(`${botId}-message`).textContent = state?.message || "";
  $(`${botId}-kaggleStatus`).textContent = state?.kaggle_status || "—";
  $(`${botId}-workerStage`).textContent = state?.worker?.stage || "—";
  $(`${botId}-kernelRef`).textContent = state?.kernel_ref || "Not configured";
  $(`${botId}-runtime`).textContent = formatRuntime(state?.worker?.runtime_seconds);
  $(`${botId}-heartbeat`).textContent = state?.heartbeat_age_seconds === null || state?.heartbeat_age_seconds === undefined
    ? "—"
    : `${Math.round(state.heartbeat_age_seconds)}s ago`;

  const names = state?.worker?.gpu_names || [];
  const count = state?.worker?.gpu_count;
  $(`${botId}-gpuInfo`).textContent = count ? `${count} × ${names.join(", ") || "GPU"}` : "—";

  const dot = $(`${botId}-statusDot`);
  dot.className = `dot ${phaseClass(phase)}`;

  const busy = ["starting", "submitting", "waiting_worker", "worker_starting", "online", "stopping"].includes(phase);
  $(`${botId}-startBtn`).disabled = busy && phase !== "heartbeat_lost";
  $(`${botId}-stopBtn`).disabled = phase === "offline";

  if (state?.auto_stop_remaining_seconds !== null && state?.auto_stop_remaining_seconds !== undefined) {
    $(`${botId}-autoStopText`).textContent = `Auto-stop in ${formatRuntime(state.auto_stop_remaining_seconds)}`;
  } else {
    $(`${botId}-autoStopText`).textContent = "No active timer";
  }

  const workerLog = state?.worker_log || "Waiting for worker...";
  const controllerLog = (state?.controller_log || []).join("\n") || "Ready.";
  if (workerLog !== lastWorkerLog[botId]) {
    $(`${botId}-workerLog`).textContent = workerLog;
    $(`${botId}-workerLog`).scrollTop = $(`${botId}-workerLog`).scrollHeight;
    lastWorkerLog[botId] = workerLog;
  }
  if (controllerLog !== lastControllerLog[botId]) {
    $(`${botId}-controllerLog`).textContent = controllerLog;
    $(`${botId}-controllerLog`).scrollTop = $(`${botId}-controllerLog`).scrollHeight;
    lastControllerLog[botId] = controllerLog;
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
    if (data.ok) {
      for (const botId of BOT_IDS) renderBot(botId, data.bots?.[botId] || {});
    }
  } catch (err) {
    for (const botId of BOT_IDS) {
      $(`${botId}-message`).textContent = "Controller connection lost. Retrying...";
    }
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

for (const botId of BOT_IDS) {
  $(`${botId}-startBtn`).addEventListener("click", async () => {
    try {
      $(`${botId}-startBtn`).disabled = true;
      const minutes = Number($(`${botId}-autoStop`).value || 0);
      const data = await post(`/api/start/${botId}`, { auto_stop_minutes: minutes });
      showToast(data.message || "Start requested");
      await refresh();
    } catch (err) {
      showToast(err.message);
      await refresh();
    }
  });

  $(`${botId}-stopBtn`).addEventListener("click", async () => {
    const label = botId === "subtitle" ? "Subtitle bot" : "Encoding bot";
    if (!confirm(`Stop the ${label} and end its Kaggle worker run?`)) return;
    try {
      $(`${botId}-stopBtn`).disabled = true;
      const data = await post(`/api/stop/${botId}`);
      showToast(data.message || "Stop requested");
      await refresh();
    } catch (err) {
      showToast(err.message);
      await refresh();
    }
  });
}

refresh();
setInterval(refresh, 3000);
