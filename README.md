# Kaggle On-Demand Subtitle Telegram Bot Controller

A small private web dashboard that starts the user's Kaggle GPU worker only when needed, reports startup stages, confirms when the Telegram bot is live, and stops the bot/run on demand.

## Features
- Private login
- START / STOP buttons
- Kaggle CLI launch
- Live worker heartbeat
- GPU count/names
- Bot runtime
- Worker + controller logs
- Safety auto-stop timer
- Stop works during setup commands and while `main.py` is running
- Existing Telegram/GitHub/OpenAI credentials remain in Kaggle Secrets

## Quick start
Read `SETUP_GUIDE.md` first.

Local install:
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```

## Architecture

```text
Browser
  |
  v
Control Website (Flask)
  |  START -> kaggle kernels push
  |
  +<---- heartbeat / command polling ----+
                                        |
                                 Kaggle GPU Worker
                                        |
                                 Subtitle_Gen/main.py
                                        |
                                   Telegram API
```

The STOP button does not merely hide the website state. The Kaggle worker receives the stop command, terminates the bot process group, then exits so the Kaggle run can finish.
