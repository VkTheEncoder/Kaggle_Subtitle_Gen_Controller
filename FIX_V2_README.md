# Fixed V2 — Why the previous worker failed and how this version fixes it

## Root cause
The previous controller starts Kaggle with `kaggle kernels push`. Kaggle API-pushed versions do not reliably inherit User Secrets that were attached in the Notebook editor. The previous `worker.py` tried to read `CONTROLLER_URL` and `CONTROL_SECRET` at module import time, before `main()` and before the first heartbeat. Therefore an API-pushed version could fail immediately even though the checkboxes were enabled in the editor.

This exactly matches the observed symptom: Kaggle gets T4 x2 and briefly RUNNING, then ERROR, while the dashboard remains `Waiting for worker...` with no heartbeat.

## What V2 changes
V2 does not use `kaggle_secrets` for API-started workers.

1. Render validates the bot credentials before launching Kaggle.
2. Render generates a short-lived HMAC-signed bootstrap token and injects only the Render URL + short-lived token into the private worker source.
3. The Kaggle worker calls `/api/worker/bootstrap` over HTTPS.
4. Render verifies the short-lived token and returns the runtime credentials plus a per-run signed secret.
5. The worker then sends heartbeats/status and runs the exact bot setup flow.

The permanent `CONTROL_SECRET` itself is never written into the Kaggle worker source.

## Render environment variables required
Keep:
- KAGGLE_API_TOKEN
- KAGGLE_KERNEL_REF=thevk3/subtitle-bot-controller-worker
- KAGGLE_KERNEL_TITLE=Subtitle Bot Controller Worker
- KAGGLE_ACCELERATOR=NvidiaTeslaT4
- PUBLIC_BASE_URL=https://kaggle-subtitle-gen-controller.onrender.com
- CONTROL_SECRET=<strong random secret>
- DASHBOARD_USERNAME / DASHBOARD_PASSWORD
- FLASK_SECRET_KEY

Add the bot runtime credentials to Render:
- GITHUB_TOKEN
- API_ID
- API_HASH
- BOT_TOKEN
- ALLOWED_USER_ID

Optional if your bot uses them:
- OPENAI_API_KEY
- ADMIN_IDS
- CHANNEL_MAP
- LEECH_URL

## Kaggle Secrets
The automated V2 worker does not depend on Kaggle User Secrets. You may leave your existing Kaggle Secrets in place for your original manual notebook; V2 simply does not read them.

## Deploy
Replace your current project/repository files with this V2 project, push to GitHub, and redeploy Render. Then add the five required bot runtime credentials above to Render Environment and press Start Bot.

Expected controller sequence:
- Requested accelerator: NvidiaTeslaT4
- Kernel version ... successfully pushed
- KernelWorkerStatus.RUNNING
- `Kaggle worker bootstrap connected for generation ...`
- Worker stage: boot
- Worker stage: gpu_check (2 x Tesla T4)
- Worker stage: credentials
- clone_repo ...

If a later setup stage fails, the worker is already connected by then, so the actual stage error and log tail will appear on the dashboard instead of only `KernelWorkerStatus.ERROR`.
