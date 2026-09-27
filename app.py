import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKER_DIR = BASE_DIR / "kaggle_worker"
STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/kaggle_multi_bot_controller_state.json"))

BOT_DEFS = {
    "subtitle": {
        "name": "Subtitle Gen Bot",
        "worker_source": WORKER_DIR / "worker.py",
        "kernel_ref_env": "SUBTITLE_KAGGLE_KERNEL_REF",
        "legacy_kernel_ref_env": "KAGGLE_KERNEL_REF",
        "kernel_title_env": "SUBTITLE_KAGGLE_KERNEL_TITLE",
        "legacy_kernel_title_env": "KAGGLE_KERNEL_TITLE",
        "default_title": "Subtitle Bot Controller Worker",
        "accelerator_env": "SUBTITLE_KAGGLE_ACCELERATOR",
        "legacy_accelerator_env": "KAGGLE_ACCELERATOR",
    },
    "encoding": {
        "name": "Encoding Bot",
        "worker_source": WORKER_DIR / "encoding_worker.py",
        "kernel_ref_env": "ENCODING_KAGGLE_KERNEL_REF",
        "kernel_title_env": "ENCODING_KAGGLE_KERNEL_TITLE",
        "default_title": "Encoding Bot Controller Worker",
        "accelerator_env": "ENCODING_KAGGLE_ACCELERATOR",
        "default_accelerator": "NvidiaTeslaT4",
    },
}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
)

STATE_LOCK = threading.RLock()
START_LOCKS = {bot_id: threading.Lock() for bot_id in BOT_DEFS}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_bot_state():
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
        "error_logs_fetched_generation": None,
    }


def default_state():
    return {"bots": {bot_id: default_bot_state() for bot_id in BOT_DEFS}}


def load_state():
    state = default_state()
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                if isinstance(loaded.get("bots"), dict):
                    for bot_id in BOT_DEFS:
                        saved = loaded["bots"].get(bot_id)
                        if isinstance(saved, dict):
                            state["bots"][bot_id].update(saved)
                else:
                    # One-time migration from the original single-bot controller state.
                    state["bots"]["subtitle"].update(loaded)
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


def bot_state(bot_id):
    if bot_id not in BOT_DEFS:
        raise KeyError(bot_id)
    return STATE["bots"][bot_id]


def mutate_bot_state(bot_id, **updates):
    with STATE_LOCK:
        bot_state(bot_id).update(updates)
        save_state()


def append_controller_log(bot_id, text):
    if not text:
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    with STATE_LOCK:
        state = bot_state(bot_id)
        logs = list(state.get("controller_log", []))
        for line in str(text).splitlines():
            line = line.strip()
            if line:
                logs.append(f"[{stamp}] {line}")
        state["controller_log"] = logs[-220:]
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


def _control_secret():
    value = os.getenv("CONTROL_SECRET", "").strip()
    if not value:
        raise RuntimeError("CONTROL_SECRET is missing on the controller.")
    return value


def _sign_value(value):
    return hmac.new(
        _control_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_bootstrap_token(bot_id, generation):
    issued = int(time.time())
    body = f"{bot_id}:{int(generation)}:{issued}"
    sig = _sign_value("bootstrap:" + body)
    raw = f"{body}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def validate_bootstrap_token(token, bot_id, generation, max_age=1800):
    try:
        padded = str(token) + "=" * (-len(str(token)) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        token_bot_id, gen_text, issued_text, sig = raw.split(":", 3)
        gen = int(gen_text)
        issued = int(issued_text)
        if token_bot_id != bot_id or gen != int(generation):
            return False
        if abs(time.time() - issued) > max_age:
            return False
        expected = _sign_value(f"bootstrap:{bot_id}:{gen}:{issued}")
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def run_secret_for(bot_id, generation):
    return _sign_value(f"run:{bot_id}:{int(generation)}")


def verify_worker_secret(bot_id):
    if bot_id not in BOT_DEFS:
        return False
    provided = request.headers.get("X-Run-Secret", "")
    generation = request.headers.get("X-Run-Generation", "")
    try:
        generation = int(generation)
    except Exception:
        return False
    expected = run_secret_for(bot_id, generation)
    if not provided or not hmac.compare_digest(str(provided), str(expected)):
        return False
    with STATE_LOCK:
        return generation == int(bot_state(bot_id).get("generation") or 0)


def runtime_config_from_env(bot_id):
    if bot_id == "subtitle":
        required = ["GITHUB_TOKEN", "API_ID", "API_HASH", "BOT_TOKEN", "ALLOWED_USER_ID"]
        optional = ["OPENAI_API_KEY", "ADMIN_IDS", "CHANNEL_MAP", "LEECH_URL"]
        config = {}
        missing = []
        for key in required + optional:
            value = os.getenv(key, "").strip()
            if value:
                config[key] = value
            elif key in required:
                missing.append(key)
        if missing:
            raise RuntimeError(
                "Missing Render runtime credentials for Subtitle bot: " + ", ".join(missing)
            )
        return config

    if bot_id == "encoding":
        # Reuse the existing GitHub token by default. A separate token is optional.
        github_token = os.getenv("ENCODING_GITHUB_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip()
        if not github_token:
            raise RuntimeError(
                "Missing GitHub token for Encoding bot. Set GITHUB_TOKEN or ENCODING_GITHUB_TOKEN on Render."
            )
        return {"GITHUB_TOKEN": github_token}

    raise RuntimeError(f"Unknown bot id: {bot_id}")


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


def _bot_env_value(bot_id, primary_key, legacy_key=None, default=""):
    value = os.getenv(primary_key, "").strip()
    if not value and legacy_key:
        value = os.getenv(legacy_key, "").strip()
    return value or default


def kernel_ref(bot_id):
    cfg = BOT_DEFS[bot_id]
    ref = _bot_env_value(
        bot_id,
        cfg["kernel_ref_env"],
        cfg.get("legacy_kernel_ref_env"),
    ).strip("/")
    if "/" not in ref:
        raise RuntimeError(
            f"{cfg['kernel_ref_env']} must look like username/{bot_id}-bot-controller-worker"
        )
    return ref


def kernel_title(bot_id):
    cfg = BOT_DEFS[bot_id]
    return _bot_env_value(
        bot_id,
        cfg["kernel_title_env"],
        cfg.get("legacy_kernel_title_env"),
        cfg["default_title"],
    )


def accelerator_for(bot_id):
    cfg = BOT_DEFS[bot_id]
    return _bot_env_value(
        bot_id,
        cfg["accelerator_env"],
        cfg.get("legacy_accelerator_env"),
        cfg.get("default_accelerator", ""),
    )


def prepare_worker_directory(bot_id, generation):
    cfg = BOT_DEFS[bot_id]
    ref = kernel_ref(bot_id)
    title = kernel_title(bot_id)
    accelerator = accelerator_for(bot_id)

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_base_url.startswith("https://"):
        raise RuntimeError(
            "PUBLIC_BASE_URL must be the HTTPS Render URL, e.g. https://your-service.onrender.com"
        )

    # Validate credentials before consuming a Kaggle GPU session.
    runtime_config_from_env(bot_id)

    workdir = Path(tempfile.mkdtemp(prefix=f"{bot_id}-kaggle-worker-"))
    worker_text = cfg["worker_source"].read_text(encoding="utf-8")
    worker_text = worker_text.replace("__CONTROLLER_URL_JSON__", json.dumps(public_base_url))
    worker_text = worker_text.replace(
        "__BOOTSTRAP_TOKEN_JSON__", json.dumps(make_bootstrap_token(bot_id, generation))
    )
    worker_text = worker_text.replace("__RUN_GENERATION_JSON__", json.dumps(int(generation)))
    worker_text = worker_text.replace("__BOT_ID_JSON__", json.dumps(bot_id))

    if bot_id == "encoding":
        repo_owner = os.getenv("ENCODING_REPO_OWNER", "VkTheEncoder").strip() or "VkTheEncoder"
        repo_name = os.getenv("ENCODING_REPO_NAME", "Queue3-GPU").strip() or "Queue3-GPU"
        entrypoint = os.getenv("ENCODING_BOT_ENTRYPOINT", "muxbot.py").strip() or "muxbot.py"
        worker_text = worker_text.replace("__REPO_OWNER_JSON__", json.dumps(repo_owner))
        worker_text = worker_text.replace("__REPO_NAME_JSON__", json.dumps(repo_name))
        worker_text = worker_text.replace("__ENTRYPOINT_JSON__", json.dumps(entrypoint))

    (workdir / "worker.py").write_text(worker_text, encoding="utf-8")

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


def run_cli(bot_id, command, timeout=120):
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
            append_controller_log(bot_id, output)
        raise TimeoutError(f"Command timed out after {timeout}s") from exc

    output = result.stdout or ""
    if output:
        append_controller_log(bot_id, output)
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


def poll_kaggle_status(bot_id, generation):
    poll_seconds = max(5, int(os.getenv("KAGGLE_STATUS_POLL_SECONDS", "12")))
    while True:
        with STATE_LOCK:
            state = bot_state(bot_id)
            if generation != state.get("generation"):
                return
            desired = state.get("desired_state")
            phase = state.get("phase")
        try:
            rc, output = run_cli(
                bot_id, ["kaggle", "kernels", "status", kernel_ref(bot_id)], timeout=45
            )
            status = parse_kaggle_status(output)
            fetch_error_logs = False
            with STATE_LOCK:
                state = bot_state(bot_id)
                state["kaggle_status"] = status if rc == 0 else "status_error"
                if status == "error" and phase not in {"offline", "stopping"}:
                    state["phase"] = "error"
                    state["message"] = "Kaggle worker run failed. Fetching Kaggle traceback..."
                    if state.get("error_logs_fetched_generation") != generation:
                        state["error_logs_fetched_generation"] = generation
                        fetch_error_logs = True
                elif status == "complete":
                    if desired == "stopped":
                        state["phase"] = "offline"
                        state["message"] = "Stopped. Kaggle run completed."
                    elif phase not in {"online", "error"}:
                        state["phase"] = "offline"
                        state["message"] = "Kaggle run finished before the bot became live."
                save_state()

            if fetch_error_logs:
                append_controller_log(bot_id, "Kaggle reported ERROR; fetching the kernel traceback...")
                try:
                    run_cli(bot_id, ["kaggle", "kernels", "logs", kernel_ref(bot_id)], timeout=90)
                except Exception as exc:
                    append_controller_log(bot_id, f"Could not fetch Kaggle error log: {exc}")
        except Exception as exc:
            append_controller_log(bot_id, f"Status check warning: {exc}")

        with STATE_LOCK:
            state = bot_state(bot_id)
            if generation != state.get("generation"):
                return
            if state.get("desired_state") == "stopped" and state.get("phase") == "offline":
                return
        time.sleep(poll_seconds)


def submit_kaggle_job(bot_id, generation):
    lock = START_LOCKS[bot_id]
    if not lock.acquire(blocking=False):
        append_controller_log(bot_id, "Another start operation is already in progress.")
        return
    worker_dir = None
    try:
        append_controller_log(bot_id, "Preparing Kaggle worker package...")
        worker_dir, accelerator = prepare_worker_directory(bot_id, generation)

        command = ["kaggle", "kernels", "push", "-p", str(worker_dir)]
        if accelerator:
            command.extend(["--accelerator", accelerator])

        mutate_bot_state(
            bot_id,
            phase="submitting",
            message="Submitting worker to Kaggle...",
            kaggle_status="submitting",
        )
        append_controller_log(bot_id, "Submitting private worker kernel to Kaggle.")
        if accelerator:
            append_controller_log(bot_id, f"Requested accelerator: {accelerator}")
        else:
            append_controller_log(
                bot_id, "No accelerator override requested; using/preserving Kaggle kernel setting."
            )

        timeout = int(os.getenv("KAGGLE_PUSH_TIMEOUT_SECONDS", "300"))
        rc, _ = run_cli(bot_id, command, timeout=timeout)
        if rc != 0:
            mutate_bot_state(
                bot_id,
                phase="error",
                message="Kaggle submission failed. Check controller logs.",
                kaggle_status="submit_error",
            )
            return

        with STATE_LOCK:
            state = bot_state(bot_id)
            if generation != state.get("generation"):
                return
            state["phase"] = "waiting_worker"
            state["message"] = "Kaggle started. Waiting for worker heartbeat..."
            state["kaggle_status"] = "queued"
            save_state()
        threading.Thread(
            target=poll_kaggle_status, args=(bot_id, generation), daemon=True
        ).start()
    except Exception as exc:
        append_controller_log(bot_id, f"START ERROR: {exc}")
        mutate_bot_state(bot_id, phase="error", message=str(exc), kaggle_status="error")
    finally:
        if worker_dir:
            shutil.rmtree(worker_dir, ignore_errors=True)
        lock.release()


def watchdog_loop():
    while True:
        try:
            now = time.time()
            with STATE_LOCK:
                stale_seconds = int(os.getenv("WORKER_HEARTBEAT_STALE_SECONDS", "45"))
                for bot_id in BOT_DEFS:
                    state = bot_state(bot_id)
                    auto_stop_at = state.get("auto_stop_at")
                    desired = state.get("desired_state")
                    phase = state.get("phase")
                    last_hb = float(state.get("last_heartbeat") or 0)

                    if auto_stop_at and desired == "running" and now >= float(auto_stop_at):
                        state["desired_state"] = "stopped"
                        state["phase"] = "stopping"
                        state["message"] = "Auto-stop reached. Asking Kaggle worker to exit..."
                        state["stop_requested_at"] = utc_now_iso()
                        state["auto_stop_at"] = None
                        append_controller_log(bot_id, "Auto-stop timer reached; stop command issued.")

                    if phase == "online" and last_hb and (now - last_hb) > stale_seconds:
                        state["phase"] = "heartbeat_lost"
                        state["message"] = "Worker heartbeat lost; checking Kaggle status."
                save_state()
        except Exception:
            # Do not recursively log here because logging also takes STATE_LOCK.
            pass
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
    return render_template("index.html", bots=BOT_DEFS)


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": utc_now_iso()})


@app.route("/api/status")
@dashboard_auth_required
def api_status():
    with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE))
    now = time.time()
    for bot_id, state in snapshot["bots"].items():
        hb = float(state.get("last_heartbeat") or 0)
        state["heartbeat_age_seconds"] = round(now - hb, 1) if hb else None
        if state.get("auto_stop_at"):
            state["auto_stop_remaining_seconds"] = max(
                0, int(float(state["auto_stop_at"]) - now)
            )
        else:
            state["auto_stop_remaining_seconds"] = None
        try:
            state["kernel_ref"] = kernel_ref(bot_id)
        except Exception:
            state["kernel_ref"] = "Not configured"
        state["display_name"] = BOT_DEFS[bot_id]["name"]
    return jsonify({"ok": True, "bots": snapshot["bots"]})


@app.route("/api/start/<bot_id>", methods=["POST"])
@dashboard_auth_required
def api_start(bot_id):
    if bot_id not in BOT_DEFS:
        return jsonify({"ok": False, "error": "Unknown bot."}), 404

    data = request.get_json(silent=True) or {}
    raw_minutes = data.get("auto_stop_minutes", os.getenv("DEFAULT_AUTO_STOP_MINUTES", "120"))
    try:
        auto_minutes = int(raw_minutes) if raw_minutes not in (None, "", 0, "0") else 0
        auto_minutes = max(0, min(auto_minutes, 720))
    except Exception:
        auto_minutes = 120

    with STATE_LOCK:
        state = bot_state(bot_id)
        if state.get("desired_state") == "running" and state.get("phase") not in {
            "offline", "error", "heartbeat_lost"
        }:
            return jsonify({"ok": False, "error": f"{BOT_DEFS[bot_id]['name']} is already starting or running."}), 409

        state["generation"] = int(state.get("generation", 0)) + 1
        generation = state["generation"]
        state["desired_state"] = "running"
        state["phase"] = "starting"
        state["message"] = "Start requested. Preparing Kaggle job..."
        state["kaggle_status"] = "preparing"
        state["last_heartbeat"] = 0.0
        state["last_heartbeat_iso"] = None
        state["worker"] = {}
        state["worker_log"] = ""
        state["controller_log"] = []
        state["start_requested_at"] = utc_now_iso()
        state["stop_requested_at"] = None
        state["auto_stop_at"] = time.time() + auto_minutes * 60 if auto_minutes else None
        state["error_logs_fetched_generation"] = None
        save_state()

    threading.Thread(
        target=submit_kaggle_job, args=(bot_id, generation), daemon=True
    ).start()
    return jsonify({"ok": True, "message": f"{BOT_DEFS[bot_id]['name']} start accepted."})


@app.route("/api/stop/<bot_id>", methods=["POST"])
@dashboard_auth_required
def api_stop(bot_id):
    if bot_id not in BOT_DEFS:
        return jsonify({"ok": False, "error": "Unknown bot."}), 404
    with STATE_LOCK:
        state = bot_state(bot_id)
        state["desired_state"] = "stopped"
        state["phase"] = "stopping"
        state["message"] = "Stop requested. Waiting for Kaggle worker to exit..."
        state["stop_requested_at"] = utc_now_iso()
        state["auto_stop_at"] = None
        save_state()
    append_controller_log(
        bot_id, "STOP requested from dashboard. Worker will terminate the bot and exit the Kaggle run."
    )
    return jsonify({"ok": True, "message": f"{BOT_DEFS[bot_id]['name']} stop accepted."})


@app.route("/api/worker/<bot_id>/bootstrap", methods=["POST"])
def worker_bootstrap(bot_id):
    if bot_id not in BOT_DEFS:
        return jsonify({"ok": False, "error": "Unknown bot."}), 404
    data = request.get_json(silent=True) or {}
    try:
        generation = int(data.get("generation"))
    except Exception:
        return jsonify({"ok": False, "error": "Invalid generation"}), 400

    token = request.headers.get("X-Bootstrap-Token", "")
    with STATE_LOCK:
        state = bot_state(bot_id)
        current_generation = int(state.get("generation") or 0)
        desired = state.get("desired_state")
    if generation != current_generation or desired != "running":
        return jsonify({"ok": False, "error": "This run is no longer active"}), 409
    if not validate_bootstrap_token(token, bot_id, generation):
        return jsonify({"ok": False, "error": "Invalid or expired bootstrap token"}), 403

    try:
        config = runtime_config_from_env(bot_id)
    except Exception as exc:
        append_controller_log(bot_id, f"BOOTSTRAP CONFIG ERROR: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500

    append_controller_log(bot_id, f"Kaggle worker bootstrap connected for generation {generation}.")
    return jsonify({
        "ok": True,
        "run_secret": run_secret_for(bot_id, generation),
        "config": config,
        "server_time": utc_now_iso(),
    })


@app.route("/api/worker/<bot_id>/command")
def worker_command(bot_id):
    if not verify_worker_secret(bot_id):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    with STATE_LOCK:
        desired = bot_state(bot_id).get("desired_state", "stopped")
    return jsonify({"ok": True, "desired_state": desired, "server_time": utc_now_iso()})


@app.route("/api/worker/<bot_id>/heartbeat", methods=["POST"])
def worker_heartbeat(bot_id):
    if not verify_worker_secret(bot_id):
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "starting"))[:40]
    stage = str(data.get("stage", "worker"))[:80]
    message = str(data.get("message", ""))[:500]
    log_tail = str(data.get("log_tail", ""))[-12000:]

    with STATE_LOCK:
        state = bot_state(bot_id)
        state["last_heartbeat"] = time.time()
        state["last_heartbeat_iso"] = utc_now_iso()
        state["worker"] = {
            "status": status,
            "stage": stage,
            "message": message,
            "gpu_count": data.get("gpu_count"),
            "gpu_names": data.get("gpu_names", []),
            "bot_pid": data.get("bot_pid"),
            "runtime_seconds": data.get("runtime_seconds"),
        }
        if log_tail:
            state["worker_log"] = log_tail

        desired = state.get("desired_state")
        if status == "online" and desired == "running":
            state["phase"] = "online"
            state["message"] = message or "Telegram bot is live."
            state["kaggle_status"] = "running"
        elif status == "error":
            state["phase"] = "error"
            state["message"] = message or "Worker reported an error."
        elif status in {"stopped", "stopping"}:
            state["phase"] = "stopping" if status == "stopping" else "offline"
            state["message"] = message or ("Stopping..." if status == "stopping" else "Stopped.")
        elif desired == "running" and state.get("phase") not in {"online", "error"}:
            state["phase"] = "worker_starting"
            state["message"] = message or f"Worker stage: {stage}"
        save_state()

    return jsonify({"ok": True, "desired_state": bot_state(bot_id).get("desired_state")})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
