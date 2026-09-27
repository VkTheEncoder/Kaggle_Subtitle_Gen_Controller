"""Kaggle worker for the Queue3-GPU Telegram encoding bot.

This reproduces the uploaded encoding notebook without requiring manual Kaggle
cell execution: clone repo -> install requirements -> run muxbot.py.
The Render controller supplies only the GitHub token required to clone the
private repository; the bot's own config.py remains inside that private repo.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_OWNER = __REPO_OWNER_JSON__
REPO_NAME = __REPO_NAME_JSON__
ENTRYPOINT = __ENTRYPOINT_JSON__
REPO_PATH = Path(f"/kaggle/working/{REPO_NAME}")
BOT_LOG = Path("/kaggle/working/encoding-bot.log")
STAGE_LOG = Path("/kaggle/working/encoding-controller-stage.log")
POLL_SECONDS = 5
HEARTBEAT_SECONDS = 10

CONTROLLER_URL = __CONTROLLER_URL_JSON__.rstrip("/")
BOOTSTRAP_TOKEN = __BOOTSTRAP_TOKEN_JSON__
RUN_GENERATION = __RUN_GENERATION_JSON__
BOT_ID = __BOT_ID_JSON__

RUN_SECRET = ""
RUNTIME_CONFIG = {}


class StopRequested(Exception):
    pass


def raw_http_json(method, path, payload=None, headers=None, timeout=20):
    url = CONTROLLER_URL + path
    data = None
    req_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else {}


def bootstrap_from_controller():
    global RUN_SECRET, RUNTIME_CONFIG
    result = raw_http_json(
        "POST",
        f"/api/worker/{BOT_ID}/bootstrap",
        payload={"generation": RUN_GENERATION},
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        timeout=30,
    )
    if not result.get("ok"):
        raise RuntimeError("Controller bootstrap rejected the Kaggle worker.")
    RUN_SECRET = str(result.get("run_secret") or "").strip()
    config = result.get("config") or {}
    if not RUN_SECRET:
        raise RuntimeError("Controller bootstrap did not provide a run secret.")
    if not isinstance(config, dict):
        raise RuntimeError("Controller bootstrap returned an invalid runtime config.")
    RUNTIME_CONFIG = {str(k): str(v) for k, v in config.items() if v is not None}


def http_json(method, path, payload=None, timeout=8):
    if not RUN_SECRET:
        raise RuntimeError("Worker has not completed controller bootstrap.")
    return raw_http_json(
        method,
        path,
        payload=payload,
        headers={
            "X-Run-Secret": RUN_SECRET,
            "X-Run-Generation": str(RUN_GENERATION),
        },
        timeout=timeout,
    )


def runtime_secret(name, required=False, default=None):
    value = RUNTIME_CONFIG.get(name)
    if value is not None:
        value = str(value).strip()
    if value:
        return value
    if required:
        raise RuntimeError(f"Required runtime credential '{name}' was not supplied by controller.")
    return default


def command_state():
    try:
        result = http_json("GET", f"/api/worker/{BOT_ID}/command")
        return result.get("desired_state", "running")
    except Exception as exc:
        print(f"Controller command check warning: {exc}", flush=True)
        return "running"


def check_stop():
    if command_state() == "stopped":
        raise StopRequested("Stop requested from controller.")


def tail_file(path, max_lines=80, max_chars=12000):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])[-max_chars:]
    except Exception:
        return ""


def heartbeat(stage, status="starting", message="", gpu_count=None, gpu_names=None,
              bot_pid=None, runtime_seconds=None, log_path=None):
    payload = {
        "stage": stage,
        "status": status,
        "message": message,
        "gpu_count": gpu_count,
        "gpu_names": gpu_names or [],
        "bot_pid": bot_pid,
        "runtime_seconds": runtime_seconds,
        "log_tail": tail_file(log_path) if log_path else "",
    }
    try:
        http_json("POST", f"/api/worker/{BOT_ID}/heartbeat", payload=payload)
    except Exception as exc:
        print(f"Heartbeat warning: {exc}", flush=True)


def terminate_process_group(proc, grace=15):
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    deadline = time.time() + grace
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.5)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def run_controlled(command, stage, message, cwd=None, env=None, shell=False, check=True):
    STAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with STAGE_LOG.open("a", encoding="utf-8", buffering=1) as log:
        print(f"\n===== {stage}: {message} =====", file=log, flush=True)
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            shell=shell,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        last_hb = 0
        while proc.poll() is None:
            try:
                check_stop()
            except StopRequested:
                terminate_process_group(proc)
                raise
            now = time.time()
            if now - last_hb >= HEARTBEAT_SECONDS:
                heartbeat(stage, "starting", message, log_path=STAGE_LOG)
                last_hb = now
            time.sleep(POLL_SECONDS)
        rc = proc.returncode
    if check and rc != 0:
        raise RuntimeError(f"Stage '{stage}' failed with exit code {rc}. See stage log on dashboard.")
    return rc


def detect_gpus():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return len(names), names


def clone_repository():
    github_token = runtime_secret("GITHUB_TOKEN", required=True)
    clone_url = f"https://{REPO_OWNER}:{github_token}@github.com/{REPO_OWNER}/{REPO_NAME}.git"
    if REPO_PATH.exists():
        shutil.rmtree(REPO_PATH, ignore_errors=True)
    run_controlled(
        ["git", "clone", clone_url, str(REPO_PATH)],
        "clone_repo",
        f"Cloning private repository {REPO_OWNER}/{REPO_NAME}...",
    )


def install_dependencies():
    requirements = REPO_PATH / "requirements.txt"
    if not requirements.exists():
        raise RuntimeError(f"requirements.txt not found in {REPO_OWNER}/{REPO_NAME}.")
    run_controlled(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        "install_requirements",
        "Installing Encoding Bot requirements...",
        cwd=REPO_PATH,
    )


def verify_runtime(gpu_count, gpu_names):
    if gpu_count < 1:
        raise RuntimeError(
            "No NVIDIA GPU was allocated. Open the dedicated Encoding worker kernel once and set its accelerator to GPU T4 x2."
        )
    run_controlled(
        "nvidia-smi; ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'hevc_nvenc|h264_nvenc' || true",
        "gpu_ffmpeg_check",
        f"Detected {gpu_count} GPU(s). Checking FFmpeg/NVENC support...",
        shell=True,
        check=False,
    )
    heartbeat(
        "gpu_ready",
        "starting",
        f"GPU ready: {gpu_count} × {', '.join(gpu_names)}. Repository setup complete.",
        gpu_count=gpu_count,
        gpu_names=gpu_names,
        log_path=STAGE_LOG,
    )


def start_bot(gpu_count, gpu_names):
    entry = REPO_PATH / ENTRYPOINT
    if not entry.exists():
        raise RuntimeError(f"Encoding bot entrypoint not found: {ENTRYPOINT}")

    BOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_file = BOT_LOG.open("a", encoding="utf-8", buffering=1)
    bot_process = subprocess.Popen(
        [sys.executable, "-u", ENTRYPOINT],
        cwd=str(REPO_PATH),
        env=os.environ.copy(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    started = time.time()
    time.sleep(3)
    if bot_process.poll() is not None:
        log_file.close()
        raise RuntimeError(
            f"{ENTRYPOINT} exited immediately with code {bot_process.returncode}. Check bot log."
        )

    heartbeat(
        "bot_running",
        "online",
        "Encoding Telegram bot is live and ready to use.",
        gpu_count=gpu_count,
        gpu_names=gpu_names,
        bot_pid=bot_process.pid,
        runtime_seconds=0,
        log_path=BOT_LOG,
    )

    last_hb = 0
    try:
        while bot_process.poll() is None:
            if command_state() == "stopped":
                heartbeat(
                    "bot_stopping",
                    "stopping",
                    "Stop received. Terminating Encoding Telegram bot...",
                    gpu_count=gpu_count,
                    gpu_names=gpu_names,
                    bot_pid=bot_process.pid,
                    runtime_seconds=int(time.time() - started),
                    log_path=BOT_LOG,
                )
                terminate_process_group(bot_process)
                heartbeat(
                    "stopped",
                    "stopped",
                    "Encoding bot stopped. Kaggle worker is exiting so the GPU session can end.",
                    gpu_count=gpu_count,
                    gpu_names=gpu_names,
                    runtime_seconds=int(time.time() - started),
                    log_path=BOT_LOG,
                )
                return 0

            now = time.time()
            if now - last_hb >= HEARTBEAT_SECONDS:
                heartbeat(
                    "bot_running",
                    "online",
                    "Encoding Telegram bot is live and ready to use.",
                    gpu_count=gpu_count,
                    gpu_names=gpu_names,
                    bot_pid=bot_process.pid,
                    runtime_seconds=int(now - started),
                    log_path=BOT_LOG,
                )
                last_hb = now
            time.sleep(POLL_SECONDS)
    finally:
        if bot_process.poll() is None:
            terminate_process_group(bot_process)
        log_file.close()

    raise RuntimeError(f"Encoding Telegram bot exited unexpectedly with code {bot_process.returncode}.")


def main():
    gpu_count = 0
    gpu_names = []
    try:
        bootstrap_from_controller()
        heartbeat("boot", "starting", "Encoding worker connected. Checking Kaggle GPU...")
        check_stop()

        subprocess.run(["python", "--version"], check=False)
        subprocess.run(["nvidia-smi"], check=False)
        gpu_count, gpu_names = detect_gpus()
        heartbeat(
            "gpu_check",
            "starting",
            f"Detected {gpu_count} GPU(s): {', '.join(gpu_names) if gpu_names else 'none'}",
            gpu_count=gpu_count,
            gpu_names=gpu_names,
        )

        check_stop()
        clone_repository()

        check_stop()
        install_dependencies()

        check_stop()
        verify_runtime(gpu_count, gpu_names)

        check_stop()
        return start_bot(gpu_count, gpu_names)

    except StopRequested as exc:
        if RUN_SECRET:
            heartbeat(
                "stopped",
                "stopped",
                str(exc),
                gpu_count=gpu_count,
                gpu_names=gpu_names,
                log_path=STAGE_LOG,
            )
        print(str(exc), flush=True)
        return 0
    except Exception as exc:
        if RUN_SECRET:
            heartbeat(
                "error",
                "error",
                str(exc),
                gpu_count=gpu_count,
                gpu_names=gpu_names,
                log_path=BOT_LOG if BOT_LOG.exists() else STAGE_LOG,
            )
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
