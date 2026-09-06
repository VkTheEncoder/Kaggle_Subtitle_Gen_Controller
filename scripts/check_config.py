import os
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

required = [
    "DASHBOARD_PASSWORD",
    "FLASK_SECRET_KEY",
    "CONTROL_SECRET",
    "PUBLIC_BASE_URL",
    "KAGGLE_KERNEL_REF",
]
missing = [key for key in required if not os.getenv(key)]

has_token = bool(os.getenv("KAGGLE_API_TOKEN"))
has_legacy = bool(os.getenv("KAGGLE_USERNAME") and os.getenv("KAGGLE_KEY"))
if not (has_token or has_legacy):
    missing.append("KAGGLE_API_TOKEN OR KAGGLE_USERNAME+KAGGLE_KEY")

if missing:
    print("Missing configuration:")
    for key in missing:
        print(" -", key)
    raise SystemExit(1)

print("Environment configuration looks complete.")
print("Testing Kaggle authentication...")
proc = subprocess.run(
    ["kaggle", "kernels", "list", "-m", "--page-size", "1"],
    text=True,
    capture_output=True,
)
print(proc.stdout)
if proc.returncode != 0:
    print(proc.stderr, file=sys.stderr)
    raise SystemExit(proc.returncode)
print("Kaggle authentication OK.")
