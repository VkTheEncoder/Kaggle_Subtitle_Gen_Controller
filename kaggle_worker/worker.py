"""
Kaggle worker for the Subtitle_Gen Telegram bot.

This script mirrors the user's working Kaggle notebook flow:
- verify GPUs
- clone VkTheEncoder/Subtitle_Gen from private GitHub
- load Kaggle Secrets
- install aria2 + Python 3.10 venv
- install PaddlePaddle GPU + requirements
- clear old bot/OCR processes + PaddleOCR cache
- warm up PaddleOCR on GPU 0
- validate GPU 1
- start main.py
- heartbeat to the controller
- stop cleanly when the controller says STOP
"""

import gc
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from kaggle_secrets import UserSecretsClient

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

secrets_client = UserSecretsClient()


def secret(name, required=False, default=None):
    try:
        value = secrets_client.get_secret(name)
    except Exception:
        value = None
    if value is not None:
        value = str(value).strip()
    if value:
        return value
    if required:
        raise RuntimeError(f"Required Kaggle Secret '{name}' is missing or not enabled for this notebook.")
    return default


CONTROLLER_URL = secret("CONTROLLER_URL", required=True).rstrip("/")
CONTROL_SECRET = secret("CONTROL_SECRET", required=True)


class StopRequested(Exception):
    pass


def http_json(method, path, payload=None, timeout=8):
    url = CONTROLLER_URL + path
    data = None
    headers = {"X-Control-Secret": CONTROL_SECRET}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else {}


def command_state():
    try:
        result = http_json("GET", "/api/worker/command")
        return result.get("desired_state", "running")
    except Exception as exc:
        print(f"Controller command check warning: {exc}", flush=True)
        # Fail-open: a brief controller/network interruption should not kill the bot.
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
        value = secret(key, required=False)
        if value:
            os.environ[key] = value
        elif key in required:
            missing.append(key)
    if missing:
        raise RuntimeError("Missing required Kaggle Secrets: " + ", ".join(missing))

    # Same stable settings used in the uploaded notebook.
    os.environ.setdefault("OCR_WORKERS", "2")
    os.environ.setdefault("ENCODE_CONCURRENCY", "2")
    os.environ.setdefault("OCR_SCAN_WIDTH", "1280")
    os.environ.setdefault("FRBOT_BASE_DIR", "/kaggle/working/frbot")


def clone_repository():
    github_token = secret("GITHUB_TOKEN", required=True)
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
    # Re-read required credentials exactly before launch, matching the notebook's final cell behavior.
    for key in ["API_ID", "API_HASH", "BOT_TOKEN", "ALLOWED_USER_ID"]:
        bot_env[key] = secret(key, required=True)
    for key in ["OPENAI_API_KEY", "CHANNEL_MAP", "LEECH_URL", "ADMIN_IDS"]:
        value = secret(key, required=False)
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
        heartbeat("boot", "starting", "Kaggle worker started. Checking GPU...")
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

        # Your original notebook explicitly uses GPU 0 and GPU 1.
        if gpu_count < 2:
            raise RuntimeError(
                "This bot setup expects 2 GPUs because the working notebook uses PaddleOCR on GPU 0 and GPU 1. "
                "Open the dedicated Kaggle worker once and select the same GPU T4 x2 accelerator, then start again."
            )

        check_stop()
        heartbeat("secrets", "starting", "Loading Kaggle bot secrets...")
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
