# AI Fitness Trainer Bot

## Overview
Personal fitness trainer Telegram bot powered by Claude AI + Oura Ring data + Apple Watch integration.
Single-user bot for postpartum recovery and strength training.

## Tech Stack
- **Python 3.12** with python-telegram-bot[job-queue]
- **Claude API** (anthropic SDK) — workout generation and coaching
- **Oura Ring API v2** — sleep, readiness, activity data
- **OpenAI Whisper** — voice message transcription (optional)
- **SQLite** — local data storage (trainer_data.db)
- **Railway** — deployment platform

## Project Structure
```
telegram-bot-fitness/
├── bot.py                        # Main bot code
├── Apple Watch Shortcut Guide.md # iOS Shortcuts setup guide
├── requirements.txt              # Python dependencies
├── Procfile                      # Railway deployment config
├── runtime.txt                   # Python version for Railway
├── Dockerfile                    # Docker deployment
├── .env.example                  # Environment variable template
├── .gitignore                    # Git ignore rules
└── CLAUDE.md                     # This file
```

## Database Tables (SQLite: trainer_data.db)
- **workout_log** — exercise, weight, sets_reps, notes, date
- **conversation_history** — role + content (last 50 messages)
- **settings** — key/value store (apple health data, pelvic floor status)
- **training_week** — deload cycle tracking (week 1-4, cycle_start_date)
- **progression_snapshots** — biweekly lift numbers + pilates milestones

## Commands
- `/start` — Welcome + setup
- `/checkin` — Daily check-in (Oura + Apple Watch + buttons)
- `/week` — Weekly training plan
- `/log` — Log workout (e.g., `/log squat 60kg 3x8`)
- `/progress` — View progress with trend arrows
- `/remind` — Set morning reminder time
- `/health` — Accept Apple Watch data (from Shortcuts)
- `/deload` — Check/reset deload cycle
- `/updateprogress` — Store progression snapshot
- `/help` — All commands

## Scheduled Jobs
- **Morning reminder** — daily at user-set time
- **Sunday weekly plan** — auto-sends weekly plan at 9 AM local
- **Biweekly progression check-in** — every 14 days asks for updated numbers
- **Training week advance** — every Sunday advances the 4-week deload cycle

## Key Patterns
- All handlers check `is_authorized()` first
- Scheduled jobs are guarded by `if ALLOWED_USER_ID:`
- `ask_claude()` auto-injects: progress context, deload week, pelvic floor status
- Conversation history kept to last 50 messages
- Oura data fetched on-demand (not cached)

## Environment Variables
```
TELEGRAM_TOKEN=         # Required
ANTHROPIC_API_KEY=      # Required
OURA_TOKEN=             # Recommended
ALLOWED_USER_ID=        # Recommended (restricts to one user)
OPENAI_API_KEY=         # Optional (voice messages)
REMINDER_HOUR=7         # Default morning reminder hour
REMINDER_MINUTE=0       # Default morning reminder minute
TIMEZONE_OFFSET=-4      # EDT = -4
```

## Local Development
```bash
pip install -r requirements.txt
cp .env.example .env  # Fill in your tokens
python bot.py
```

## Deployment
Deployed on Railway, auto-deploys on git push.
```bash
git add .
git commit -m "Description"
git push
```
