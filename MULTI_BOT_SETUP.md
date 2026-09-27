# Multi-Bot Kaggle Controller — Subtitle + Encoding Bot

This version keeps the existing Subtitle Gen controller and adds a completely independent Encoding Bot controller on the same Render website.

## What START Encoding does

1. Render authenticates to Kaggle with the same Kaggle API credentials already used by the Subtitle controller.
2. Render pushes a private dedicated Kaggle script kernel for the Encoding bot.
3. The Kaggle worker detects the allocated NVIDIA GPU(s).
4. It clones `VkTheEncoder/Queue3-GPU` (configurable with env vars).
5. It runs `pip install -r requirements.txt`.
6. It checks `nvidia-smi` and FFmpeg NVENC availability.
7. It starts `python muxbot.py`.
8. Heartbeats, GPU info and bot logs appear on the Render dashboard.
9. STOP terminates the `muxbot.py` process group and exits the Kaggle run.

This intentionally replaces the manual notebook-cell flow. The uploaded notebook contains a folder-name mismatch (`Queue3-GPU` is cloned but `Queue3-4K-GPU` is used in `%cd`); the worker uses one consistent repo path and does not depend on notebook cells.

---

## Render: what you already have and can keep

Keep these existing values unchanged:

- `DASHBOARD_USERNAME`
- `DASHBOARD_PASSWORD`
- `FLASK_SECRET_KEY`
- `CONTROL_SECRET`
- `PUBLIC_BASE_URL`
- `KAGGLE_USERNAME` + `KAGGLE_KEY` (or `KAGGLE_API_TOKEN`)
- `GITHUB_TOKEN`
- Existing Subtitle bot API/token settings

Your current legacy `KAGGLE_KERNEL_REF`, `KAGGLE_KERNEL_TITLE` and `KAGGLE_ACCELERATOR` still work for the Subtitle bot as fallbacks.

---

## Render: new variables to add for the Encoding bot

Add:

```env
ENCODING_KAGGLE_KERNEL_REF=YOUR_KAGGLE_USERNAME/encoding-bot-controller-worker
ENCODING_KAGGLE_KERNEL_TITLE=Encoding Bot Controller Worker
ENCODING_KAGGLE_ACCELERATOR=NvidiaTeslaT4
ENCODING_REPO_OWNER=VkTheEncoder
ENCODING_REPO_NAME=Queue3-GPU
ENCODING_BOT_ENTRYPOINT=muxbot.py
```

`ENCODING_KAGGLE_KERNEL_REF` must be different from the Subtitle worker kernel ref.

### GitHub token

If your existing `GITHUB_TOKEN` can read private repo `VkTheEncoder/Queue3-GPU`, add nothing else.

If you want a different token for the Encoding repo, add:

```env
ENCODING_GITHUB_TOKEN=YOUR_TOKEN
```

The Encoding worker prefers `ENCODING_GITHUB_TOKEN`, then falls back to `GITHUB_TOKEN`.

### Telegram bot credentials for Encoding

The uploaded encoding notebook only clones the private repo and runs `muxbot.py`. Therefore this controller does the same. If the real private `Queue3-GPU` repo already contains the private `config.py` used by your manual Kaggle notebook, **no new Telegram credential is required on Render or Kaggle**.

If that private repo does not contain `config.py`, put back whatever configuration source your manually working notebook uses before expecting the controller to start the bot. This patch does not guess or recreate your removed config file.

---

## Kaggle: one-time setup for the Encoding worker

You do NOT need to edit the uploaded encoding notebook.

1. Deploy this patched controller to Render.
2. Add the new Render environment variables above.
3. Open the website and press **Start Encoding**.
4. The controller creates/pushes the private kernel slug from `ENCODING_KAGGLE_KERNEL_REF`.
5. With `ENCODING_KAGGLE_ACCELERATOR=NvidiaTeslaT4`, the Kaggle CLI requests the **GPU T4 x2** accelerator automatically.
6. The worker verifies the actual GPU allocation with `nvidia-smi` and shows the GPU count/names on the dashboard.
7. From then onward use only the website Start/Stop buttons.

The Encoding worker requires at least one NVIDIA GPU. `NvidiaTeslaT4` is Kaggle's current accelerator ID for the T4 x2 machine shape. Allocation still depends on your Kaggle GPU quota/availability. Your encoding bot itself decides whether an encode uses NVENC or CPU based on the codec selected in Telegram.

---

## Kaggle Secrets

For the Encoding bot, no new Kaggle User Secret is required by this controller. The API-pushed worker securely bootstraps its GitHub token from Render, just like the fixed controller architecture.

Do not add Kaggle API username/key to a notebook. Keep Kaggle API authentication on Render.

---

## Recommended optional cleanup for Subtitle variables

You may leave your old variables exactly as they are. Or, later, rename them for clarity:

```env
SUBTITLE_KAGGLE_KERNEL_REF=YOUR_KAGGLE_USERNAME/subtitle-bot-controller-worker
SUBTITLE_KAGGLE_KERNEL_TITLE=Subtitle Bot Controller Worker
SUBTITLE_KAGGLE_ACCELERATOR=
```

If these are absent, the controller falls back to your old:

```env
KAGGLE_KERNEL_REF
KAGGLE_KERNEL_TITLE
KAGGLE_ACCELERATOR
```

---

## First end-to-end test

1. Redeploy Render after adding env vars.
2. Login to the controller.
3. Confirm both cards appear: Subtitle Gen Bot and Encoding Bot.
4. Start only Encoding Bot first with a 30-minute auto-stop.
5. Watch stages: `STARTING` → `SUBMITTING` → `WAITING WORKER` → `GPU CHECK` → `CLONE REPO` → `INSTALL REQUIREMENTS` → `GPU/FFMPEG CHECK` → `ONLINE`.
6. Open Telegram and use the Encoding bot.
7. Test one CPU encode and one GPU/NVENC encode.
8. Press **Stop Encoding**.
9. Confirm the Encoding card goes offline without changing the Subtitle card.
10. Then test starting/stopping Subtitle independently.

You can start both independently, subject to your Kaggle account's concurrent-session/GPU quota.
