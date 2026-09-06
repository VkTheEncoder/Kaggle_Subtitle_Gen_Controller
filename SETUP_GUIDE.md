# Subtitle Gen Bot Controller — Exact Setup Guide

This controller is built around the uploaded working Kaggle notebook. It keeps the bot **on demand**: press **START** when you want it, and **STOP** when the work is finished.

## What the controller does

START:
1. Authenticates to Kaggle from the control website backend.
2. Pushes/starts a private Kaggle worker kernel.
3. The worker checks for the same 2-GPU setup used by the current notebook.
4. Clones `VkTheEncoder/Subtitle_Gen` from private GitHub.
5. Loads the same bot secrets from Kaggle Secrets.
6. Installs `aria2`, Python 3.10 venv, PaddlePaddle GPU and `requirements.txt`.
7. Clears old bot/OCR processes and PaddleOCR cache.
8. Warms PaddleOCR on GPU 0.
9. Tests PaddleOCR on GPU 1.
10. Starts `main.py`.
11. Website changes to **ONLINE** after the worker heartbeat confirms `main.py` is alive.

STOP:
1. Website changes desired state to `stopped`.
2. Kaggle worker sees the stop command on its next poll.
3. It sends SIGTERM/SIGKILL to the `main.py` process group if needed.
4. The worker exits.
5. Kaggle run finishes, so the GPU session can be released.

The controller also supports a safety auto-stop timer.

---

# 1. Required credentials

## A. Kaggle credentials for the CONTROL WEBSITE

You need either:

### Compatibility method (recommended)
`KAGGLE_USERNAME` and `KAGGLE_KEY` from your Kaggle API credentials / `kaggle.json`.

Example:
```json
{"username":"yourname","key":"xxxxxxxx"}
```

Then use:
```env
KAGGLE_USERNAME=yourname
KAGGLE_KEY=xxxxxxxx
```

### Or newer token method
If your Kaggle CLI/account uses an API token directly:
```env
KAGGLE_API_TOKEN=your-token
```

Do **not** put Kaggle API credentials inside the Kaggle notebook itself. They belong only on the controller backend.

---

## B. Existing Kaggle Secrets for the BOT

Your current notebook already uses these. Keep/add them in **Kaggle > Notebook > Add-ons / Secrets** and make sure they are enabled for the dedicated worker kernel.

Required:
- `GITHUB_TOKEN` — GitHub token that can clone private repo `VkTheEncoder/Subtitle_Gen`.
- `API_ID` — Telegram API ID.
- `API_HASH` — Telegram API hash.
- `BOT_TOKEN` — Telegram bot token from BotFather.
- `ALLOWED_USER_ID` — allowed Telegram user ID used by your bot.

Optional (only if your repo uses them):
- `OPENAI_API_KEY`
- `ADMIN_IDS`
- `CHANNEL_MAP`
- `LEECH_URL`

---

## C. Two NEW Kaggle Secrets for website control

After the website is deployed, add these two secrets to Kaggle:

### `CONTROLLER_URL`
Your public controller URL, for example:
```text
https://subtitle-bot-controller.onrender.com
```
Do not include a trailing slash.

### `CONTROL_SECRET`
A long random secret. It must be **exactly the same** as the `CONTROL_SECRET` environment variable on the website backend.

You can generate safe values with:
```bash
python scripts/generate_secrets.py
```

---

# 2. Configure the controller

Copy:
```text
.env.example
```
to:
```text
.env
```

The app automatically loads a local `.env` file. For Render/Railway/etc., enter the same values in the host's Environment Variables page.

Minimum controller variables:

```env
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your-private-dashboard-password
FLASK_SECRET_KEY=long-random-value
CONTROL_SECRET=long-random-value
PUBLIC_BASE_URL=https://your-controller-domain

KAGGLE_USERNAME=your-kaggle-username
KAGGLE_KEY=your-kaggle-api-key

KAGGLE_KERNEL_REF=your-kaggle-username/subtitle-bot-controller-worker
KAGGLE_KERNEL_TITLE=Subtitle Bot Controller Worker
KAGGLE_ACCELERATOR=
```

`CONTROL_SECRET` must also be copied into Kaggle Secrets.

---

# 3. Important: preserve the same 2 × T4 behavior

Your uploaded working notebook explicitly uses **GPU 0 and GPU 1**, and the screenshot shows two Tesla T4 GPUs. The worker therefore intentionally refuses to continue if Kaggle gives it fewer than 2 GPUs. This prevents a silent "looks online but encoding later fails" situation.

Recommended one-time setup:

1. Use a dedicated kernel ref such as:
   `yourusername/subtitle-bot-controller-worker`
2. Start it once from the controller. This creates the private worker kernel if it does not already exist.
3. If the first run reports only 1 GPU, open that worker kernel on Kaggle once.
4. Set its accelerator to the same **GPU T4 x2** option you use in the current notebook.
5. Save that setting.
6. Keep `KAGGLE_ACCELERATOR=` blank in the controller so the controller does not intentionally override the worker kernel's existing accelerator setting.
7. Press START again.

If your Kaggle CLI/account can select the exact accelerator you want programmatically, you may set `KAGGLE_ACCELERATOR` to a supported accelerator ID, but verify the actual GPU count shown on the dashboard.

---

# 4. Deploy the control website

The backend must have a public HTTPS URL because the Kaggle worker calls it for heartbeat and STOP commands.

## Render example

1. Upload this project to a private GitHub repo.
2. In Render create a new Web Service from that repo.
3. Build command:
   ```bash
   pip install -r requirements.txt
   ```
4. Start command:
   ```bash
   gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0
   ```
5. Add all controller environment variables from `.env.example`.
6. Deploy.
7. Copy the final public HTTPS URL.
8. Set that URL as `PUBLIC_BASE_URL` on the controller.
9. Add the same URL as Kaggle Secret `CONTROLLER_URL`.
10. Redeploy/restart the controller after changing environment variables.

`render.yaml` is included if you prefer blueprint deployment.

---

# 5. First test

1. Open your website.
2. Login with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.
3. Set auto-stop to 30 minutes for the first test.
4. Press **START BOT**.
5. Expected phases:
   - STARTING
   - SUBMITTING
   - WAITING WORKER
   - GPU CHECK
   - SECRETS
   - CLONE REPO
   - SYSTEM DEPENDENCIES
   - PADDLE GPU0
   - PADDLE GPU1
   - GPU/FFMPEG CHECK
   - ONLINE
6. When it says **ONLINE**, send a normal command/file to your Telegram bot.
7. When finished, press **STOP BOT**.
8. Dashboard should show STOPPING and then OFFLINE after the worker exits and Kaggle completes the run.

---

# 6. Local test option

You can run the website locally:

Windows:
```bat
start_local.bat
```

Linux/macOS:
```bash
./start_local.sh
```

Before local HTTP testing, set `COOKIE_SECURE=0` in `.env`.

The site opens at:
```text
http://127.0.0.1:5000
```

However, Kaggle cannot call `127.0.0.1` on your computer. For a full local end-to-end test, expose port 5000 using a public tunnel such as your existing ngrok setup and use that HTTPS tunnel URL as `CONTROLLER_URL`.

For everyday use, a permanently hosted controller URL is easier.

---

# 7. Security rules

- Keep the controller repository private if you commit any deployment configuration.
- Never commit `.env`, `kaggle.json`, Telegram tokens, API hashes, or GitHub tokens.
- Keep `CONTROL_SECRET` different from the dashboard password.
- `GITHUB_TOKEN`, `BOT_TOKEN`, `API_HASH`, etc. stay in Kaggle Secrets and are never sent to the web dashboard.
- The worker sends only status, GPU information and log tails to the controller.
- The dedicated Kaggle kernel is marked private in its metadata.

---

# 8. If START fails

Check the Controller Log first.

Common causes:
- Invalid `KAGGLE_USERNAME` / `KAGGLE_KEY`.
- Wrong `KAGGLE_KERNEL_REF`.
- Kaggle GPU quota exhausted.
- Kaggle allocated only one GPU instead of the two-GPU setup.
- `GITHUB_TOKEN` missing or cannot read the private repository.
- Bot credential missing from Kaggle Secrets.
- `CONTROLLER_URL` or `CONTROL_SECRET` does not match.
- Internet disabled on the worker kernel (the controller metadata requests internet enabled).

---

# 9. If STOP is pressed during installation

The worker checks the website during long shell commands. It terminates the active installation/warm-up command and exits. Therefore STOP is not limited to the final Telegram-bot stage.

If Kaggle itself is completely frozen or cannot reach the controller, use Kaggle's own session/run stop control as the emergency fallback.
