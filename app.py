import json
import os
import hmac
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKER_SOURCE = BASE_DIR / "kaggle_worker" / "worker.py"
STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/subtitle_bot_controller_state.json"))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
)

STATE_LOCK = threading.RLock()
START_LOCK = threading.Lock()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_state():
    return {
        "desired_state": "stopped",
        "phase": "offline",
        "message": "Ready",
        "kaggle_status": "unknown",
        "last_heartbeat": 0.0,
        "last_heartbeat_iso": None,
        "worker": {},
        "worker_log": "",
        "controller_log": [],
        "start_requested_at": None,
        "stop_requested_at": None,
        "auto_stop_at": None,
        "generation": 0,
    }


def load_state():
    with STATE_LOCK:
        state = default_state()
        try:
            if STATE_FILE.exists():
                loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
        except Exception:
            pass
        return state


STATE = load_state()


def save_state():
    with STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)


def mutate_state(**updates):
    with STATE_LOCK:
        STATE.update(updates)
        save_state()


def append_controller_log(text):
    if not text:
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    with STATE_LOCK:
        logs = list(STATE.get("controller_log", []))
        for line in str(text).splitlines():
            line = line.strip()
            if line:
                logs.append(f"[{stamp}] {line}")
        STATE["controller_log"] = logs[-220:]
        save_state()


def dashboard_auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def verify_worker_secret():
    expected = os.getenv("CONTROL_SECRET", "")
    provided = request.headers.get("X-Control-Secret", "")
    if not provided:
        provided = request.args.get("secret", "")
    if not provided and request.is_json:
        provided = (request.get_json(silent=True) or {}).get("secret", "")
    return bool(expected) and hmac.compare_digest(str(provided), str(expected))


def kaggle_env():
    env = os.environ.copy()
    token = os.getenv("KAGGLE_API_TOKEN", "").strip()
    username = os.getenv("KAGGLE_USERNAME", "").strip()
    key = os.getenv("KAGGLE_KEY", "").strip()

    if token:
        env["KAGGLE_API_TOKEN"] = token
    elif username and key:
        env["KAGGLE_USERNAME"] = username
        env["KAGGLE_KEY"] = key
    else:
        raise RuntimeError(
            "Kaggle credentials are missing. Set KAGGLE_API_TOKEN or both "
            "KAGGLE_USERNAME and KAGGLE_KEY."
        )
    return env


def kernel_ref():
    ref = os.getenv("KAGGLE_KERNEL_REF", "").strip().strip("/")
    if "/" not in ref:
        raise RuntimeError(
            "KAGGLE_KERNEL_REF must look like username/subtitle-bot-controller-worker"
        )
    return ref


def prepare_worker_directory():
    ref = kernel_ref()
    title = os.getenv("KAGGLE_KERNEL_TITLE", "Subtitle Bot Controller Worker").strip()
    accelerator = os.getenv("KAGGLE_ACCELERATOR", "").strip()

    workdir = Path(tempfile.mkdtemp(prefix="subtitle-kaggle-worker-"))
    shutil.copy2(WORKER_SOURCE, workdir / "worker.py")

    metadata = {
        "id": ref,
        "title": title,
        "code_file": "worker.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "machine_shape": accelerator,
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    (workdir / "kernel-metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    return workdir, accelerator


def run_cli(command, timeout=120):
    env = kaggle_env()
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        if output:
            append_controller_log(output)
        raise TimeoutError(f"Command timed out after {timeout}s") from exc

    output = result.stdout or ""
    if output:
        append_controller_log(output)
    return result.returncode, output


def parse_kaggle_status(text):
    low = (text or "").lower()
    for status in [
        "cancel_acknowledged",
        "cancel_requested",
        "queued",
        "running",
        "complete",
        "error",
        "new_script",
    ]:
        if status in low:
            return status
    if "successful" in low or "complete" in low:
        return "complete"
    if "failed" in low:
        return "error"
    return "unknown"


def poll_kaggle_status(generation):
    poll_seconds = max(5, int(os.getenv("KAGGLE_STATUS_POLL_SECONDS", "12")))
    while True:
        with STATE_LOCK:
            if generation != STATE.get("generation"):
                return
            desired = STATE.get("desired_state")
            phase = STATE.get("phase")
        try:
            rc, output = run_cli(
                ["kaggle", "kernels", "status", kernel_ref()], timeout=45
            )
            status = parse_kaggle_status(output)
            with STATE_LOCK:
                STATE["kaggle_status"] = status if rc == 0 else "status_error"
                if status == "error" and phase not in {"offline", "stopping"}:
                    STATE["phase"] = "error"
                    STATE["message"] = "Kaggle worker run failed. Check logs."
                elif status == "complete":
                    if desired == "stopped":
                        STATE["phase"] = "offline"
                        STATE["message"] = "Stopped. Kaggle run completed."
                    elif phase not in {"online", "error"}:
                        STATE["phase"] = "offline"
                        STATE["message"] = "Kaggle run finished before the bot became live."
                save_state()
        except Exception as exc:
            append_controller_log(f"Status check warning: {exc}")

        with STATE_LOCK:
            if generation != STATE.get("generation"):
                return
            if STATE.get("desired_state") == "stopped" and STATE.get("phase") == "offline":
                return
        time.sleep(poll_seconds)


def submit_kaggle_job(generation):
    if not START_LOCK.acquire(blocking=False):
        append_controller_log("Another start operation is already in progress.")
        return
    worker_dir = None
    try:
        append_controller_log("Preparing Kaggle worker package...")
        worker_dir, accelerator = prepare_worker_directory()

        command = ["kaggle", "kernels", "push", "-p", str(worker_dir)]
        if accelerator:
            command.extend(["--accelerator", accelerator])

        mutate_state(
            phase="submitting",
            message="Submitting worker to Kaggle...",
            kaggle_status="submitting",
        )
        append_controller_log("Submitting private worker kernel to Kaggle.")
        if accelerator:
            append_controller_log(f"Requested accelerator: {accelerator}")
        else:
            append_controller_log("No accelerator override requested; using/preserving Kaggle kernel setting.")

        timeout = int(os.getenv("KAGGLE_PUSH_TIMEOUT_SECONDS", "300"))
        rc, output = run_cli(command, timeout=timeout)
        if rc != 0:
            mutate_state(
                phase="error",
                message="Kaggle submission failed. Check controller logs.",
                kaggle_status="submit_error",
            )
            return

        with STATE_LOCK:
            if generation != STATE.get("generation"):
                return
            STATE["phase"] = "waiting_worker"
            STATE["message"] = "Kaggle started. Waiting for worker heartbeat..."
            STATE["kaggle_status"] = "queued"
            save_state()
        threading.Thread(
            target=poll_kaggle_status, args=(generation,), daemon=True
        ).start()
    except Exception as exc:
        append_controller_log(f"START ERROR: {exc}")
        mutate_state(phase="error", message=str(exc), kaggle_status="error")
    finally:
        if worker_dir:
            shutil.rmtree(worker_dir, ignore_errors=True)
        START_LOCK.release()


def watchdog_loop():
    while True:
        try:
            now = time.time()
            with STATE_LOCK:
                auto_stop_at = STATE.get("auto_stop_at")
                desired = STATE.get("desired_state")
                phase = STATE.get("phase")
                last_hb = float(STATE.get("last_heartbeat") or 0)
                stale_seconds = int(os.getenv("WORKER_HEARTBEAT_STALE_SECONDS", "45"))

                if auto_stop_at and desired == "running" and now >= float(auto_stop_at):
                    STATE["desired_state"] = "stopped"
                    STATE["phase"] = "stopping"
                    STATE["message"] = "Auto-stop reached. Asking Kaggle worker to exit..."
                    STATE["stop_requested_at"] = utc_now_iso()
                    STATE["auto_stop_at"] = None
                    append_controller_log("Auto-stop timer reached; stop command issued.")
                    save_state()

                if phase == "online" and last_hb and (now - last_hb) > stale_seconds:
                    STATE["phase"] = "heartbeat_lost"
                    STATE["message"] = "Worker heartbeat lost; checking Kaggle status."
                    save_state()
        except Exception as exc:
            append_controller_log(f"Watchdog warning: {exc}")
        time.sleep(5)


threading.Thread(target=watchdog_loop, daemon=True).start()


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        expected_user = os.getenv("DASHBOARD_USERNAME", "admin")
        expected_password = os.getenv("DASHBOARD_PASSWORD", "")
        if (
            expected_password
            and hmac.compare_digest(username, expected_user)
            and hmac.compare_digest(password, expected_password)
        ):
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
@dashboard_auth_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@dashboard_auth_required
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": utc_now_iso()})


@app.route("/api/status")
@dashboard_auth_required
def api_status():
    with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE))
    now = time.time()
    hb = float(snapshot.get("last_heartbeat") or 0)
    snapshot["heartbeat_age_seconds"] = round(now - hb, 1) if hb else None
    if snapshot.get("auto_stop_at"):
        snapshot["auto_stop_remaining_seconds"] = max(
            0, int(float(snapshot["auto_stop_at"]) - now)
        )
    else:
        snapshot["auto_stop_remaining_seconds"] = None
    snapshot["kernel_ref"] = os.getenv("KAGGLE_KERNEL_REF", "")
    return jsonify({"ok": True, "state": snapshot})


@app.route("/api/start", methods=["POST"])
@dashboard_auth_required
def api_start():
    data = request.get_json(silent=True) or {}
    raw_minutes = data.get("auto_stop_minutes", os.getenv("DEFAULT_AUTO_STOP_MINUTES", "120"))
    try:
        auto_minutes = int(raw_minutes) if raw_minutes not in (None, "", 0, "0") else 0
        auto_minutes = max(0, min(auto_minutes, 720))
    except Exception:
        auto_minutes = 120

    with STATE_LOCK:
        if STATE.get("desired_state") == "running" and STATE.get("phase") not in {"offline", "error", "heartbeat_lost"}:
            return jsonify({"ok": False, "error": "Bot is already starting or running."}), 409

        STATE["generation"] = int(STATE.get("generation", 0)) + 1
        generation = STATE["generation"]
        STATE["desired_state"] = "running"
        STATE["phase"] = "starting"
        STATE["message"] = "Start requested. Preparing Kaggle job..."
        STATE["kaggle_status"] = "preparing"
        STATE["last_heartbeat"] = 0.0
        STATE["last_heartbeat_iso"] = None
        STATE["worker"] = {}
        STATE["worker_log"] = ""
        STATE["controller_log"] = []
        STATE["start_requested_at"] = utc_now_iso()
        STATE["stop_requested_at"] = None
        STATE["auto_stop_at"] = time.time() + auto_minutes * 60 if auto_minutes else None
        save_state()

    threading.Thread(target=submit_kaggle_job, args=(generation,), daemon=True).start()
    return jsonify({"ok": True, "message": "Start command accepted."})


@app.route("/api/stop", methods=["POST"])
@dashboard_auth_required
def api_stop():
    with STATE_LOCK:
        STATE["desired_state"] = "stopped"
        STATE["phase"] = "stopping"
        STATE["message"] = "Stop requested. Waiting for Kaggle worker to exit..."
        STATE["stop_requested_at"] = utc_now_iso()
        STATE["auto_stop_at"] = None
        save_state()
    append_controller_log("STOP requested from dashboard. Worker will terminate the bot and exit the Kaggle run.")
    return jsonify({"ok": True, "message": "Stop command accepted."})


@app.route("/api/worker/command")
def worker_command():
    if not verify_worker_secret():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    with STATE_LOCK:
        desired = STATE.get("desired_state", "stopped")
    return jsonify({"ok": True, "desired_state": desired, "server_time": utc_now_iso()})


@app.route("/api/worker/heartbeat", methods=["POST"])
def worker_heartbeat():
    if not verify_worker_secret():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "starting"))[:40]
    stage = str(data.get("stage", "worker"))[:80]
    message = str(data.get("message", ""))[:500]
    log_tail = str(data.get("log_tail", ""))[-12000:]

    with STATE_LOCK:
        STATE["last_heartbeat"] = time.time()
        STATE["last_heartbeat_iso"] = utc_now_iso()
        STATE["worker"] = {
            "status": status,
            "stage": stage,
            "message": message,
            "gpu_count": data.get("gpu_count"),
            "gpu_names": data.get("gpu_names", []),
            "bot_pid": data.get("bot_pid"),
            "runtime_seconds": data.get("runtime_seconds"),
        }
        if log_tail:
            STATE["worker_log"] = log_tail

        desired = STATE.get("desired_state")
        if status == "online" and desired == "running":
            STATE["phase"] = "online"
            STATE["message"] = message or "Telegram bot is live."
            STATE["kaggle_status"] = "running"
        elif status == "error":
            STATE["phase"] = "error"
            STATE["message"] = message or "Worker reported an error."
        elif status in {"stopped", "stopping"}:
            STATE["phase"] = "stopping" if status == "stopping" else "offline"
            STATE["message"] = message or ("Stopping..." if status == "stopping" else "Stopped.")
        elif desired == "running" and STATE.get("phase") not in {"online", "error"}:
            STATE["phase"] = "worker_starting"
            STATE["message"] = message or f"Worker stage: {stage}"
        save_state()

    return jsonify({"ok": True, "desired_state": STATE.get("desired_state")})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
