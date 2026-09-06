"""
Kaggle worker for the Subtitle_Gen Telegram bot.

IMPORTANT: API-triggered `kaggle kernels push` runs do not reliably inherit
Kaggle UI User Secrets. Therefore this worker does NOT depend on kaggle_secrets.
It receives a short-lived bootstrap token embedded by the Render controller,
then securely fetches the runtime credentials from the controller over HTTPS.
"""

import gc
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_OWNER = "VkTheEncoder"
REPO_NAME = "Subtitle_Gen"
REPO_PATH = Path(f"/kaggle/working/{REPO_NAME}")
VENV_PATH = Path("/kaggle/working/frbot-venv")
PYTHON_BIN = VENV_PATH / "bin" / "python"
BOT_LOG = Path("/kaggle/working/frbot.log")
STAGE_LOG = Path("/kaggle/working/controller-stage.log")
PADDLE_CACHE = Path.home() / ".paddleocr"
POLL_SECONDS = 5
HEARTBEAT_SECONDS = 10

# Replaced by the controller immediately before `kaggle kernels push`.
CONTROLLER_URL = __CONTROLLER_URL_JSON__.rstrip("/")
BOOTSTRAP_TOKEN = __BOOTSTRAP_TOKEN_JSON__
RUN_GENERATION = __RUN_GENERATION_JSON__

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
        "/api/worker/bootstrap",
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
        raise RuntimeError(f"Required runtime credential '{name}' was not supplied by the controller.")
    return default


def command_state():
    try:
        result = http_json("GET", "/api/worker/command")
        return result.get("desired_state", "running")
    except Exception as exc:
        print(f"Controller command check warning: {exc}", flush=True)
        return "running"


def check_stop():
    if command_state() == "stopped":
        raise StopRequested("Stop requested from control website.")


def tail_file(path, max_lines=80, max_chars=12000):
    try:
        if not Path(path).exists():
            return ""
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
        return "\n".join(lines)[-max_chars:]
    except Exception:
        return ""


def heartbeat(stage, status="starting", message="", gpu_count=None, gpu_names=None, bot_pid=None, runtime_seconds=None, log_path=None):
    payload = {
        "generation": RUN_GENERATION,
        "stage": stage,
        "status": status,
        "message": message,
        "gpu_count": gpu_count,
        "gpu_names": gpu_names or [],
        "bot_pid": bot_pid,
        "runtime_seconds": runtime_seconds,
        "log_tail": tail_file(log_path or STAGE_LOG),
    }
    try:
        http_json("POST", "/api/worker/heartbeat", payload)
    except Exception as exc:
        print(f"Heartbeat warning: {exc}", flush=True)


def terminate_process_group(proc, grace=15):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=grace)
        return
    except Exception:
        pass
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


def load_bot_secrets_into_environment():
    required = ["API_ID", "API_HASH", "BOT_TOKEN", "ALLOWED_USER_ID"]
    optional = ["OPENAI_API_KEY", "ADMIN_IDS", "CHANNEL_MAP", "LEECH_URL"]
    missing = []
    for key in required + optional:
        value = runtime_secret(key, required=False)
        if value:
            os.environ[key] = value
        elif key in required:
            missing.append(key)
    if missing:
        raise RuntimeError("Missing required runtime credentials: " + ", ".join(missing))

    os.environ.setdefault("OCR_WORKERS", "2")
    os.environ.setdefault("ENCODE_CONCURRENCY", "2")
    os.environ.setdefault("OCR_SCAN_WIDTH", "1280")
    os.environ.setdefault("FRBOT_BASE_DIR", "/kaggle/working/frbot")


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
    run_controlled(
        "apt-get update -qq && apt-get install -y -qq aria2 python3.10-venv",
        "system_dependencies",
        "Installing aria2 and Python 3.10 venv support...",
        shell=True,
    )
    if not PYTHON_BIN.exists():
        run_controlled(
            ["python3.10", "-m", "venv", str(VENV_PATH)],
            "create_venv",
            "Creating Python 3.10 virtual environment...",
        )

    commands = [
        [str(PYTHON_BIN), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools<81"],
        [
            str(PYTHON_BIN),
            "-m",
            "pip",
            "install",
            "paddlepaddle-gpu==2.6.1.post120",
            "-f",
            "https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html",
        ],
        [str(PYTHON_BIN), "-m", "pip", "install", "-r", "requirements.txt"],
    ]
    labels = ["pip_bootstrap", "paddle_install", "repo_requirements"]
    messages = [
        "Updating pip/wheel/setuptools...",
        "Installing PaddlePaddle GPU...",
        "Installing Subtitle_Gen requirements...",
    ]
    for command, label, message in zip(commands, labels, messages):
        run_controlled(command, label, message, cwd=REPO_PATH)


def clean_previous_state():
    subprocess.run(
        "pkill -f '/kaggle/working/frbot-venv/bin/python.*main.py'",
        shell=True,
        check=False,
    )
    subprocess.run("pkill -f 'multiprocessing.spawn'", shell=True, check=False)
    shutil.rmtree(PADDLE_CACHE, ignore_errors=True)
    heartbeat("cleanup", "starting", "Old bot/OCR processes stopped and PaddleOCR cache cleared.")


def warmup_gpu0():
    warmup_code = r'''
import gc
import numpy as np
import cv2
from paddleocr import PaddleOCR
image = np.full((180, 800, 3), 255, dtype=np.uint8)
cv2.putText(image, "PADDLE OCR TEST", (20, 115), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
print("Downloading and validating PaddleOCR models on GPU 0...")
ocr = PaddleOCR(use_angle_cls=False, lang="ch", use_gpu=True, gpu_id=0, show_log=True, det_db_box_thresh=0.5, rec_batch_num=32, cpu_threads=2)
ocr.ocr(image, cls=False)
print("GPU 0 PaddleOCR warm-up passed.")
del ocr
gc.collect()
'''
    run_controlled(
        [str(PYTHON_BIN), "-c", warmup_code],
        "paddle_gpu0",
        "Downloading/validating PaddleOCR models on GPU 0...",
    )


def test_gpu1():
    gpu1_test = r'''
import numpy as np
from paddleocr import PaddleOCR
image = np.full((100, 500, 3), 255, dtype=np.uint8)
print("Loading existing models on GPU 1...")
ocr = PaddleOCR(use_angle_cls=False, lang="ch", use_gpu=True, gpu_id=1, show_log=True, det_db_box_thresh=0.5, rec_batch_num=32, cpu_threads=2)
ocr.ocr(image, cls=False)
print("GPU 1 PaddleOCR test passed.")
'''
    run_controlled(
        [str(PYTHON_BIN), "-c", gpu1_test],
        "paddle_gpu1",
        "Validating PaddleOCR on GPU 1...",
    )


def verify_ffmpeg():
    run_controlled(
        "nvidia-smi; ffmpeg -hide_banner -encoders 2>/dev/null | grep -E 'hevc_nvenc|h264_nvenc' || true; ffmpeg -hide_banner -filters 2>/dev/null | grep scale_cuda || true",
        "gpu_ffmpeg_check",
        "Checking NVIDIA GPU and FFmpeg NVENC/CUDA support...",
        shell=True,
    )


def start_bot(gpu_count, gpu_names):
    BOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    bot_env = os.environ.copy()
    for key in ["API_ID", "API_HASH", "BOT_TOKEN", "ALLOWED_USER_ID"]:
        bot_env[key] = runtime_secret(key, required=True)
    for key in ["OPENAI_API_KEY", "CHANNEL_MAP", "LEECH_URL", "ADMIN_IDS"]:
        value = runtime_secret(key, required=False)
        if value:
            bot_env[key] = value
    bot_env["OCR_WORKERS"] = "2"
    bot_env["OCR_SCAN_WIDTH"] = "1280"
    bot_env["ENCODE_CONCURRENCY"] = "2"

    log_file = BOT_LOG.open("a", encoding="utf-8", buffering=1)
    bot_process = subprocess.Popen(
        [str(PYTHON_BIN), "-u", "main.py"],
        cwd=str(REPO_PATH),
        env=bot_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    started = time.time()
    time.sleep(3)
    if bot_process.poll() is not None:
        log_file.close()
        raise RuntimeError(
            f"main.py exited immediately with code {bot_process.returncode}. Check bot log."
        )

    heartbeat(
        "bot_running",
        "online",
        "Telegram bot is live and ready to use.",
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
                    "Stop received. Terminating Telegram bot...",
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
                    "Telegram bot stopped. Kaggle worker is exiting so the GPU session can end.",
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
                    "Telegram bot is live and ready to use.",
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

    rc = bot_process.returncode
    raise RuntimeError(f"Telegram bot exited unexpectedly with code {rc}.")


def main():
    gpu_count = 0
    gpu_names = []
    try:
        # This is deliberately first. It replaces the broken dependency on
        # Kaggle UI secrets for API-pushed versions.
        bootstrap_from_controller()
        heartbeat("boot", "starting", "Kaggle worker connected to controller. Checking GPU...")
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

        if gpu_count < 2:
            raise RuntimeError(
                "This bot setup expects 2 GPUs because the working notebook uses PaddleOCR on GPU 0 and GPU 1. "
                "Set KAGGLE_ACCELERATOR=NvidiaTeslaT4 so Kaggle allocates T4 x2."
            )

        check_stop()
        heartbeat("credentials", "starting", "Runtime credentials received securely from controller.")
        load_bot_secrets_into_environment()

        check_stop()
        clone_repository()

        check_stop()
        install_dependencies()

        check_stop()
        clean_previous_state()

        check_stop()
        warmup_gpu0()

        check_stop()
        test_gpu1()

        check_stop()
        verify_ffmpeg()

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
