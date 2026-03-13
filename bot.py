"""
🏋️ AI Fitness Trainer Telegram Bot
====================================
Personal trainer powered by Claude + Oura Ring data.

Features:
- Auto-pulls sleep/recovery data from Oura
- Morning reminder at your chosen time
- Progress tracking for lifts and exercises
- Voice message support (dictate your check-in)
- Claude generates personalized daily workouts
- 4-week deload cycle tracking
- Biweekly progression snapshots
- Sunday auto-weekly plan
- Pelvic floor symptom tracking

Commands:
  /start           - Welcome & setup
  /checkin         - Daily check-in (auto-pulls Oura data)
  /week            - Get weekly training plan
  /log             - Log workout (e.g., /log squat 60kg 3x8)
  /progress        - View progress on tracked lifts
  /remind          - Set morning reminder time
  /health          - Accept Apple Watch data (from Shortcuts)
  /deload          - Check/reset deload cycle
  /updateprogress  - Store progression snapshot
  /help            - Show all commands
"""

import os
import sys
import json
import sqlite3
import logging
import logging.handlers
import asyncio
import tempfile
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path

# ── Dependencies ──────────────────────────────────────────────
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        ContextTypes, filters
    )
except ImportError:
    sys.exit("Install: pip install python-telegram-bot[job-queue]")

try:
    import anthropic
except ImportError:
    sys.exit("Install: pip install anthropic")

try:
    import requests
except ImportError:
    sys.exit("Install: pip install requests")

try:
    import openai
except ImportError:
    openai = None  # Voice transcription will be disabled

try:
    import edge_tts
except ImportError:
    edge_tts = None  # Voice replies will be disabled

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Configuration ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OURA_TOKEN = os.environ.get("OURA_TOKEN")
VOICE_API_KEY = os.environ.get("VOICE_API_KEY") or os.environ.get("OPENAI_API_KEY")  # For Whisper STT
VOICE_API_BASE = os.environ.get("VOICE_API_BASE")  # Optional: custom endpoint (Groq, xAI)
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-JennyNeural")  # Edge TTS voice
VOICE_MODE = os.environ.get("VOICE_MODE", "both")  # "both", "text", "voice" — reply mode
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID")  # Optional: restrict to your Telegram ID
REMINDER_HOUR = int(os.environ.get("REMINDER_HOUR", "7"))  # Default 7 AM
REMINDER_MINUTE = int(os.environ.get("REMINDER_MINUTE", "0"))
TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "-4"))  # EDT = -4

for var, name in [(TELEGRAM_TOKEN, "TELEGRAM_TOKEN"), (ANTHROPIC_API_KEY, "ANTHROPIC_API_KEY")]:
    if not var:
        sys.exit(f"Error: Set {name} in environment or .env file")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DIR = Path(os.environ.get(
    "LOG_DIR",
    os.path.expanduser("~/Library/CloudStorage/OneDrive-FloridaInstituteofTechnology/telegram-bot-fitness/logs")
))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"bot_{datetime.now().strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.handlers.TimedRotatingFileHandler(
            LOG_FILE, when="midnight", backupCount=30, encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger(__name__)

# ── Database (SQLite) ─────────────────────────────────────────
DB_PATH = Path("trainer_data.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS workout_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            exercise TEXT NOT NULL,
            weight TEXT,
            sets_reps TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS training_week (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            week_number INTEGER NOT NULL DEFAULT 1,
            cycle_start_date TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS progression_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            squat TEXT,
            deadlift TEXT,
            bench_press TEXT,
            overhead_press TEXT,
            hip_thrust TEXT,
            row_exercise TEXT,
            pilates_milestones TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    result = c.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result


# ── Training Week (Deload Cycle) ─────────────────────────────

def get_current_training_week():
    """Get current training week (1-4). Week 4 = deload."""
    rows = db_execute("SELECT week_number, cycle_start_date FROM training_week WHERE id = 1", fetch=True)
    if not rows:
        today = datetime.now().strftime("%Y-%m-%d")
        db_execute("INSERT INTO training_week (id, week_number, cycle_start_date) VALUES (1, 1, ?)", (today,))
        return 1, today
    return rows[0][0], rows[0][1]


def advance_training_week():
    """Advance to next week in the 4-week cycle."""
    week, start = get_current_training_week()
    next_week = (week % 4) + 1
    if next_week == 1:
        start = datetime.now().strftime("%Y-%m-%d")
    db_execute("UPDATE training_week SET week_number = ?, cycle_start_date = ? WHERE id = 1", (next_week, start))
    return next_week


def reset_training_week():
    """Reset to week 1."""
    today = datetime.now().strftime("%Y-%m-%d")
    db_execute("UPDATE training_week SET week_number = 1, cycle_start_date = ? WHERE id = 1", (today,))
    return 1


# ── Progression Snapshots ────────────────────────────────────

def save_progression_snapshot(data):
    """Save a progression snapshot. data is a dict of lift: value."""
    today = datetime.now().strftime("%Y-%m-%d")
    db_execute(
        """INSERT INTO progression_snapshots
           (date, squat, deadlift, bench_press, overhead_press, hip_thrust, row_exercise, pilates_milestones)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (today,
         data.get("squat"), data.get("deadlift"), data.get("bench_press") or data.get("bench"),
         data.get("overhead_press") or data.get("ohp"), data.get("hip_thrust") or data.get("hipthrust"),
         data.get("row") or data.get("row_exercise"), data.get("pilates"))
    )


def get_latest_snapshots(limit=2):
    """Get the most recent progression snapshots."""
    rows = db_execute(
        """SELECT date, squat, deadlift, bench_press, overhead_press, hip_thrust, row_exercise, pilates_milestones
           FROM progression_snapshots ORDER BY id DESC LIMIT ?""",
        (limit,), fetch=True
    )
    if not rows:
        return []
    result = []
    for row in rows:
        result.append({
            "date": row[0], "squat": row[1], "deadlift": row[2],
            "bench_press": row[3], "overhead_press": row[4],
            "hip_thrust": row[5], "row": row[6], "pilates": row[7]
        })
    return result


# ── Pelvic Floor Tracking ────────────────────────────────────

def get_pelvic_floor_status():
    """Get the last reported pelvic floor status."""
    rows = db_execute("SELECT value FROM settings WHERE key = 'pelvic_floor'", fetch=True)
    return rows[0][0] if rows else None


def set_pelvic_floor_status(status):
    """Save pelvic floor status."""
    db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('pelvic_floor', ?)", (status,))


# ── Trainer System Prompt ─────────────────────────────────────
TRAINER_SYSTEM_PROMPT = """You are my personal fitness trainer, recovery coach, and applied sports scientist. Always respond in English only. Keep responses concise — this is Telegram, not an essay.

### Your Personality
You are evidence-based and precise. You think like a sports physiologist who also coaches — every recommendation has a reason grounded in exercise science, recovery research, or biomechanics. You briefly explain *why* behind each choice (e.g., "HRV is suppressed → parasympathetic load is high → today we stay aerobic"). You cite mechanisms, not vibes. You use correct terminology but keep it accessible — no jargon walls. You're calm, professional, and reassuring. You don't hype or cheerleader — you inform and guide. When data is missing, you say so honestly rather than guess. You respect that I'm educated and want to understand my own training, not just follow orders.

### About Me
- Mom of two (newborn + toddler), currently on leave
- Located in Satellite Beach, FL — ocean access year-round
- Active background: outdoor activities (running, hiking, swimming, cycling), Pilates, strength training
- Current priority: postpartum recovery + rebuilding strength
- I train daily — but intensity flexes based on recovery data

### My Home Gym
- Barbell + squat rack
- Adjustable bench (flat/incline)
- Dumbbells and kettlebells
- Cable machine and resistance bands
- Infrared sauna + pool for recovery

### Training Method: Tammy Hembrow Style
I follow Tammy Hembrow's programming approach. Use these templates as the foundation and adapt based on my recovery data, progression, and equipment. Maintain her structure (3×12 base, glute emphasis, compound-first order) but adjust load/volume using RPE.

**Glute Day A (Squat + Thrust focus)**
- Barbell Squat 3×12
- Split Squat 3×12/leg
- Wide Stance Leg Press (or Sumo Squat) 3×12
- Back Extension 3×12
- Barbell Hip Thrust 3×12
- Cable Kickback 3×15/leg

**Glute Day B (Hinge + Lunge focus)**
- Barbell Squat 3×12
- Straight Leg Deadlift 3×12
- Weighted Lunges 3×12/leg
- Smith Machine Step Up (or DB Step Up) 3×12/leg
- Sumo Squat Walk with Pulse 3×12
- Squat Jumps 3×20 (skip if PF symptoms — replace with banded squat pulses)

**Glute Day C (Thrust + Isolation focus)**
- Barbell Squat 3×12
- Barbell Hip Thrust 3×12
- Fire Hydrant 3×20/leg
- Cable Kickback 3×15/leg
- Cable Hip Abduction 3×12/leg
- Squat Pulse 40s into Squat Jump 40s (skip jumps if PF symptoms — replace with glute bridge hold)

**Upper Body**
- Seated DB Shoulder Press 3×10-12
- Arnold Press 3×10-12
- DB Side Lateral Raise 3×10-12
- Upright Cable Row 3×10-12
- Cable Front Raise w/ Rope 3×10-12
- Cable Triceps Extension 3×10-12
- Standing DB Triceps Extension 3×10-12

**Full Body**
- Reverse Grip Row 3×12
- Cable Squat 3×15
- Glute Pull Through 3×12
- Upright Row 3×12
- Single-Arm Lat Pulldown 3×12
- Bent Over Kickback 3×15
- Hip Abduction 3×12
- Kneeling Cable Crunch 3×15

**Home/HIIT (when no gym or active recovery day)**
- Jump Squat with Kickback 4×15
- Mountain Climber 4×20
- Burpees 4×10
- Bicycle Crunch 4×20
- Squat Jump Forward and Back 4×15

**Progression protocol:** Use weight where last 2-3 reps are difficult. Increase load when all sets are completed cleanly. Weeks 5-8 of each cycle: progressive overload on the same templates.

### Exercise Reference (explain form when asked)
When I ask about an exercise, provide a brief description of:
1. Setup and starting position
2. Key movement cues (2-3 bullet points)
3. Common mistakes to avoid
4. Muscles targeted
5. PF-safe modifications if relevant

Key exercises in this program:
- **Barbell Hip Thrust**: Back on bench, bar across hips, drive through heels, full hip extension, squeeze glutes at top. Avoid hyperextending lower back.
- **Cable Kickback**: Face cable machine, ankle strap, hinge slightly forward, extend hip back keeping knee slightly bent. Control the eccentric.
- **Fire Hydrant**: All fours, lift bent knee out to side to hip height. Keep core braced, avoid shifting weight.
- **Sumo Squat Walk with Pulse**: Wide stance, toes out, squat to parallel, pulse at bottom, step laterally while maintaining depth.
- **Split Squat**: Rear foot elevated (Bulgarian) or flat. Front knee tracks over toe, torso upright, lower until rear knee nearly touches ground.
- **Straight Leg Deadlift (RDL)**: Soft knee lock, hinge at hips, bar close to legs, feel hamstring stretch, squeeze glutes to return. Keep spine neutral.
- **Cable Hip Abduction**: Stand sideways to cable, strap on far ankle, lift leg away from body. Control both directions.
- **Arnold Press**: Start with palms facing you, rotate palms out as you press overhead. Full ROM shoulder press with rotation.
- **Glute Pull Through**: Face away from cable, rope between legs, hinge forward, drive hips through to standing. Similar to kettlebell swing mechanics.

### Rules
- If HRV is low or sleep was bad → lighter session or active recovery (explain the physiological reason)
- If sore from outdoor activity → adjust training to avoid overloading fatigued muscle groups
- Always include pelvic floor + deep core work on strength days (even 5 min) — postpartum diastasis/PF recovery is non-negotiable
- Never program heavy deadlifts or high-impact jumping without asking about pelvic floor symptoms
- For PF concerns: replace squat jumps and high-impact plyos with banded alternatives
- Periodize: deload every 4th week (explain the supercompensation rationale when relevant)
- If I miss days, don't guilt-trip — adjust the mesocycle forward
- Outdoor activities (running, swimming, hiking, cycling) count as cardio/conditioning — factor them into weekly volume
- When suggesting weights/reps, reference RPE or RIR so I can auto-regulate
- Weekly split: 3 glute days + 1 upper body + outdoor activity days + recovery (sauna/pool)

### Response Format for Daily Check-In
1. Recovery assessment with brief physiological reasoning (1-2 sentences)
2. Today's workout from the Tammy Hembrow templates above, adapted for today's recovery/goals (sets/reps/weight with RPE targets)
3. One adjustment note (the science behind why lighter/harder today)
Keep it under 300 words. Use emoji sparingly for readability."""


# ── Oura API ──────────────────────────────────────────────────
OURA_BASE = "https://api.ouraring.com/v2"


def fetch_oura(endpoint, start_date, end_date):
    """Fetch data from Oura API v2."""
    if not OURA_TOKEN:
        return []
    try:
        resp = requests.get(
            f"{OURA_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {OURA_TOKEN}"},
            params={"start_date": start_date, "end_date": end_date},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"Oura API error: {e}")
        return []


def get_oura_summary(date_str=None):
    """Pull today's Oura data and format as a summary string."""
    if not OURA_TOKEN:
        return "⚠️ Oura not connected. Share your data manually."

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    sleep = fetch_oura("usercollection/daily_sleep", date_str, next_day)
    readiness = fetch_oura("usercollection/daily_readiness", date_str, next_day)
    activity = fetch_oura("usercollection/daily_activity", date_str, next_day)

    parts = [f"📊 Oura data for {date_str}:"]

    if sleep:
        s = sleep[-1]
        score = s.get("score", "?")
        parts.append(f"😴 Sleep score: {score}")

    if readiness:
        r = readiness[-1]
        score = r.get("score", "?")
        contributors = r.get("contributors", {})
        hrv = contributors.get("hrv_balance", "?")
        rhr = contributors.get("resting_heart_rate", "?")
        parts.append(f"⚡ Readiness: {score} | HRV balance: {hrv} | RHR: {rhr}")

    if activity:
        a = activity[-1]
        cal = a.get("active_calories", "?")
        steps = a.get("steps", "?")
        parts.append(f"🔥 Active cal: {cal} | Steps: {steps}")

    if len(parts) == 1:
        parts.append("Data not yet available (Oura usually updates by morning)")

    return "\n".join(parts)


# ── Claude API ────────────────────────────────────────────────
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_recent_history(limit=10):
    """Get recent conversation history for context."""
    rows = db_execute(
        "SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT ?",
        (limit,), fetch=True
    )
    if not rows:
        return []
    rows.reverse()
    return [{"role": r[0], "content": r[1]} for r in rows]


def save_message(role, content):
    """Save a message to conversation history."""
    db_execute(
        "INSERT INTO conversation_history (role, content) VALUES (?, ?)",
        (role, content)
    )
    # Keep only last 50 messages
    db_execute("""
        DELETE FROM conversation_history WHERE id NOT IN (
            SELECT id FROM conversation_history ORDER BY id DESC LIMIT 50
        )
    """)


def get_progress_context():
    """Get recent workout logs for Claude's context."""
    rows = db_execute(
        "SELECT date, exercise, weight, sets_reps FROM workout_log ORDER BY date DESC LIMIT 20",
        fetch=True
    )
    if not rows:
        return ""
    lines = ["Recent workout log:"]
    for date, exercise, weight, sets_reps in rows:
        w = f" {weight}" if weight else ""
        sr = f" {sets_reps}" if sets_reps else ""
        lines.append(f"  {date}: {exercise}{w}{sr}")
    return "\n".join(lines)


def get_deload_context():
    """Get deload week context for Claude."""
    week, start = get_current_training_week()
    if week == 4:
        return (
            "\n\n### ⚠️ DELOAD WEEK (Week 4 of 4)\n"
            "This is a deload week. Reduce volume by 40-50%, reduce intensity by 20-30%. "
            "Focus on technique, mobility, and recovery. No PRs this week."
        )
    return f"\n\n### Training Cycle: Week {week} of 4 (deload on week 4)"


def get_progression_context():
    """Get progression data for Claude."""
    snapshots = get_latest_snapshots(2)
    if not snapshots:
        return ""
    current = snapshots[0]
    lines = ["\n\n### Progression Data"]
    lines.append(f"Latest snapshot ({current['date']}):")
    for lift in ["squat", "deadlift", "bench_press", "overhead_press", "hip_thrust", "row"]:
        val = current.get(lift)
        if val:
            lines.append(f"  {lift.replace('_', ' ').title()}: {val}")
    if current.get("pilates"):
        lines.append(f"  Pilates milestones: {current['pilates']}")
    return "\n".join(lines)


def get_pelvic_floor_context():
    """Get pelvic floor context for Claude."""
    status = get_pelvic_floor_status()
    if status and status != "none":
        return (
            f"\n\n### ⚠️ Pelvic Floor Alert\n"
            f"Last reported PF symptoms: {status}. "
            "Avoid heavy valsalva, high-impact jumping, heavy deadlifts. "
            "Prioritize PF-safe alternatives and include PF recovery exercises."
        )
    return ""


def ask_claude(user_message):
    """Send message to Claude with full context."""
    history = get_recent_history()
    progress = get_progress_context()

    system = TRAINER_SYSTEM_PROMPT
    if progress:
        system += f"\n\n### Recent Training Log\n{progress}"
    system += get_deload_context()
    system += get_progression_context()
    system += get_pelvic_floor_context()

    messages = history + [{"role": "user", "content": user_message}]

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=system,
            messages=messages
        )
        reply = response.content[0].text
        save_message("user", user_message)
        save_message("assistant", reply)
        return reply
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return f"Claude API error: {e}"


# ── Voice Transcription ──────────────────────────────────────
def transcribe_voice(file_path):
    """Transcribe voice message using Whisper via OpenAI-compatible API."""
    if not openai or not VOICE_API_KEY:
        return None

    try:
        client_kwargs = {"api_key": VOICE_API_KEY}
        if VOICE_API_BASE:
            client_kwargs["base_url"] = VOICE_API_BASE
        client = openai.OpenAI(**client_kwargs)

        # Use whisper-large-v3-turbo for Groq, whisper-1 for OpenAI
        model = "whisper-1"
        if VOICE_API_BASE and "groq" in VOICE_API_BASE:
            model = "whisper-large-v3-turbo"

        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                language="en"
            )
        return transcript.text
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return None


# ── Text-to-Speech ───────────────────────────────────────────
async def text_to_voice(text):
    """Convert text to voice using Edge TTS. Returns path to mp3 file or None."""
    if not edge_tts:
        return None
    try:
        # Strip markdown formatting for cleaner speech
        clean = text
        for ch in ["*", "_", "`", "#"]:
            clean = clean.replace(ch, "")

        fd, out_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        communicate = edge_tts.Communicate(clean, TTS_VOICE)
        await communicate.save(out_path)
        return out_path
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None


async def send_voice_reply(update_or_context, chat_id, text, parse_mode="Markdown"):
    """Send text reply + voice message. Adapts based on VOICE_MODE."""
    # Determine bot object
    if hasattr(update_or_context, 'message'):
        bot = update_or_context.message
        send_text = bot.reply_text
        send_voice_fn = bot.reply_voice
    else:
        # It's a context (from scheduled job)
        async def send_text(t, **kw):
            await update_or_context.bot.send_message(chat_id=chat_id, text=t, **kw)
        async def send_voice_fn(v, **kw):
            await update_or_context.bot.send_voice(chat_id=chat_id, voice=v, **kw)

    # Always send text
    if VOICE_MODE != "voice":
        await send_text(text, parse_mode=parse_mode)

    # Send voice if enabled
    if VOICE_MODE in ("both", "voice") and edge_tts:
        voice_path = await text_to_voice(text)
        if voice_path:
            try:
                with open(voice_path, "rb") as vf:
                    await send_voice_fn(vf)
            finally:
                try:
                    os.remove(voice_path)
                except OSError:
                    pass
    elif VOICE_MODE == "voice":
        # Fallback to text if TTS unavailable
        await send_text(text, parse_mode=parse_mode)


# ── Access Control ────────────────────────────────────────────
def is_authorized(update: Update) -> bool:
    """Check if user is authorized (if ALLOWED_USER_ID is set)."""
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == ALLOWED_USER_ID


# ── Bot Handlers ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ This bot is private.")
        return

    user_id = update.effective_user.id
    text = (
        "🏋️ *AI Fitness Trainer*\n\n"
        "I'm your personal trainer! Here's what I can do:\n\n"
        "📋 /checkin — morning check-in (Oura + Apple Watch + workout)\n"
        "📅 /week — weekly training plan\n"
        "💪 /log — log a workout\n"
        "📈 /progress — view progress\n"
        "⌚ /health — receive Apple Watch data\n"
        "⏰ /remind — set morning reminder\n"
        "🔄 /deload — check/reset training cycle\n"
        "📊 /updateprogress — record current lifts\n\n"
        f"💡 Your Telegram ID: `{user_id}` — "
        "add it to ALLOWED_USER_ID to make the bot private.\n\n"
        "You can also just text or send voice messages — I'll understand!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text("🔄 Pulling Oura data...")
    oura_data = get_oura_summary()

    keyboard = [
        [
            InlineKeyboardButton("⚡ Energy: low", callback_data="energy_low"),
            InlineKeyboardButton("⚡ OK", callback_data="energy_ok"),
            InlineKeyboardButton("⚡ Great", callback_data="energy_great"),
        ],
        [
            InlineKeyboardButton("🦵 No soreness", callback_data="sore_none"),
            InlineKeyboardButton("🦵 A little", callback_data="sore_mild"),
            InlineKeyboardButton("🦵 Very sore", callback_data="sore_heavy"),
        ],
        [
            InlineKeyboardButton("🌿 Outdoor: yes", callback_data="outdoor_yes"),
            InlineKeyboardButton("🌿 No", callback_data="outdoor_no"),
        ],
        [
            InlineKeyboardButton("⏱ 30 min", callback_data="time_30"),
            InlineKeyboardButton("⏱ 45 min", callback_data="time_45"),
            InlineKeyboardButton("⏱ 60 min", callback_data="time_60"),
        ],
        [
            InlineKeyboardButton("PF: none", callback_data="pf_none"),
            InlineKeyboardButton("PF: mild", callback_data="pf_mild"),
            InlineKeyboardButton("PF: concerning", callback_data="pf_concerning"),
        ],
    ]

    context.user_data["checkin"] = {"oura": oura_data, "answers": {}}
    await update.message.reply_text(
        f"{oura_data}\n\n👇 Answer the questions (tap the buttons):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_checkin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    checkin = context.user_data.get("checkin", {"oura": "", "answers": {}})
    answers = checkin["answers"]

    # Parse the callback
    mapping = {
        "energy_low": ("energy", "low"),
        "energy_ok": ("energy", "normal"),
        "energy_great": ("energy", "great"),
        "sore_none": ("soreness", "none"),
        "sore_mild": ("soreness", "mild"),
        "sore_heavy": ("soreness", "heavy"),
        "outdoor_yes": ("outdoor", "yes"),
        "outdoor_no": ("outdoor", "no"),
        "time_30": ("time", "30 min"),
        "time_45": ("time", "45 min"),
        "time_60": ("time", "60 min"),
        "pf_none": ("pelvic_floor", "none"),
        "pf_mild": ("pelvic_floor", "mild"),
        "pf_concerning": ("pelvic_floor", "concerning"),
    }

    if data in mapping:
        key, val = mapping[data]
        answers[key] = val
        # Save pelvic floor status to DB
        if key == "pelvic_floor":
            set_pelvic_floor_status(val)

    # Check if we have all answers
    needed = {"energy", "soreness", "outdoor", "time", "pelvic_floor"}
    if needed.issubset(answers.keys()):
        await query.edit_message_text("🧠 Generating workout...")

        prompt = f"{checkin['oura']}\n\n"
        apple_data = get_apple_health_today()
        if apple_data:
            prompt += f"{apple_data}\n\n"
        prompt += (
            f"Subjective check-in:\n"
            f"- Energy: {answers['energy']}\n"
            f"- Soreness: {answers['soreness']}\n"
            f"- Outdoor activity yesterday: {answers['outdoor']}\n"
            f"- Time for workout: {answers['time']}\n"
            f"- Pelvic floor: {answers['pelvic_floor']}\n\n"
            f"Give me today's workout."
        )
        reply = ask_claude(prompt)
        await query.edit_message_text(reply, parse_mode="Markdown")
        # Send voice version of the workout
        if VOICE_MODE in ("both", "voice") and edge_tts:
            voice_path = await text_to_voice(reply)
            if voice_path:
                try:
                    with open(voice_path, "rb") as vf:
                        await query.message.reply_voice(vf)
                finally:
                    try:
                        os.remove(voice_path)
                    except OSError:
                        pass
    else:
        answered = ", ".join(f"{k}: {v}" for k, v in answers.items())
        remaining = needed - answers.keys()
        await query.edit_message_text(
            f"{checkin['oura']}\n\n✅ {answered}\n\n"
            f"👇 Still need: {', '.join(remaining)}",
            reply_markup=query.message.reply_markup
        )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text("📅 Building weekly plan...")
    reply = generate_weekly_plan()
    await send_voice_reply(update, update.effective_chat.id, reply)


def generate_weekly_plan():
    """Generate a weekly training plan with full context."""
    # Pull last 7 days of Oura data for trends
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    sleep_data = fetch_oura("usercollection/daily_sleep", start, end)
    sleep_scores = [d.get("score", "?") for d in sleep_data]
    trends = f"Sleep scores last 7 days: {sleep_scores}" if sleep_scores else "No Oura trend data"

    week, cycle_start = get_current_training_week()
    deload_note = ""
    if week == 4:
        deload_note = "\n⚠️ THIS IS A DELOAD WEEK (week 4/4) — reduced volume and intensity."

    # Get progression data
    snapshots = get_latest_snapshots(1)
    progression_note = ""
    if snapshots:
        s = snapshots[0]
        lifts = []
        for lift in ["squat", "deadlift", "bench_press", "overhead_press", "hip_thrust", "row"]:
            val = s.get(lift)
            if val:
                lifts.append(f"{lift.replace('_', ' ').title()}: {val}")
        if lifts:
            progression_note = f"\nCurrent lifts: {', '.join(lifts)}"

    prompt = (
        f"Today is {datetime.now().strftime('%A, %B %d')}.\n"
        f"Training cycle: week {week}/4{deload_note}\n"
        f"Trends this week: {trends}\n"
        f"{progression_note}\n\n"
        "Build a training plan for the coming week. "
        "For each day include:\n"
        "1. Workout type (strength/Pilates/cardio/recovery)\n"
        "2. Intensity (light/moderate/heavy)\n"
        "3. Key exercises with sets/reps\n"
        "4. Schedule sauna/pool for recovery (2-3 times)\n"
        "5. Include 2-3 outdoor activity days (running, swimming, hiking, cycling)\n"
        "6. One full rest or active recovery day"
    )
    return ask_claude(prompt)


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text.replace("/log", "").strip()
    if not text:
        await update.message.reply_text(
            "💪 Log format:\n\n"
            "`/log squat 60kg 3x8`\n"
            "`/log deadlift 80kg 4x5`\n"
            "`/log hip_thrust 70kg 3x12`\n"
            "`/log plank 90sec`\n"
            "`/log running 30min`\n"
            "`/log pilates 45min`",
            parse_mode="Markdown"
        )
        return

    # Parse: exercise [weight] [sets_reps] [notes]
    parts = text.split()
    exercise = parts[0]
    weight = None
    sets_reps = None
    notes_parts = []

    for p in parts[1:]:
        if any(u in p.lower() for u in ["kg", "lb"]):
            weight = p
        elif "x" in p.lower() and any(c.isdigit() for c in p):
            sets_reps = p
        elif any(u in p.lower() for u in ["sec", "min"]):
            sets_reps = p
        else:
            notes_parts.append(p)

    notes = " ".join(notes_parts) if notes_parts else None
    today = datetime.now().strftime("%Y-%m-%d")

    db_execute(
        "INSERT INTO workout_log (date, exercise, weight, sets_reps, notes) VALUES (?, ?, ?, ?, ?)",
        (today, exercise, weight, sets_reps, notes)
    )

    await update.message.reply_text(
        f"✅ Logged: {exercise}"
        + (f" {weight}" if weight else "")
        + (f" {sets_reps}" if sets_reps else "")
        + (f" ({notes})" if notes else "")
    )


def _trend_arrow(current_val, previous_val):
    """Compare two lift values and return a trend arrow."""
    if not current_val or not previous_val:
        return ""
    try:
        # Extract numeric part (e.g., "65kg3x8" → 65)
        c_num = float(''.join(c for c in current_val.split("kg")[0].split("lb")[0] if c.isdigit() or c == '.'))
        p_num = float(''.join(c for c in previous_val.split("kg")[0].split("lb")[0] if c.isdigit() or c == '.'))
        if c_num > p_num:
            return " ↑"
        elif c_num < p_num:
            return " ↓"
        return " →"
    except (ValueError, IndexError):
        return ""


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text_parts = []

    # ── Progression Snapshots (6 key lifts with trends) ──
    snapshots = get_latest_snapshots(2)
    if snapshots:
        current = snapshots[0]
        previous = snapshots[1] if len(snapshots) > 1 else {}
        text_parts.append("📊 *Key Lifts:*\n")
        for lift in ["squat", "deadlift", "bench_press", "overhead_press", "hip_thrust", "row"]:
            c_val = current.get(lift)
            if c_val:
                arrow = _trend_arrow(c_val, previous.get(lift))
                text_parts.append(f"  *{lift.replace('_', ' ').title()}*: {c_val}{arrow}")
        if current.get("pilates"):
            text_parts.append(f"\n🧘 *Pilates:* {current['pilates']}")
        text_parts.append(f"\n_Snapshot from {current['date']}_\n")

    # ── Recent workout log ──
    rows = db_execute(
        "SELECT date, exercise, weight, sets_reps, notes FROM workout_log ORDER BY date DESC LIMIT 30",
        fetch=True
    )

    if not rows and not snapshots:
        await update.message.reply_text("📈 No entries yet. Use /log to record a workout.")
        return

    if rows:
        # Group by exercise
        exercises = {}
        for date, exercise, weight, sets_reps, notes in rows:
            ex = exercise.lower()
            if ex not in exercises:
                exercises[ex] = []
            entry = f"  {date}:"
            if weight:
                entry += f" {weight}"
            if sets_reps:
                entry += f" {sets_reps}"
            if notes:
                entry += f" ({notes})"
            exercises[ex].append(entry)

        text_parts.append("\n📈 *Recent Workouts:*\n")
        for ex, entries in exercises.items():
            text_parts.append(f"*{ex.capitalize()}*")
            text_parts.append("\n".join(entries[:5]))
            text_parts.append("")

    await update.message.reply_text("\n".join(text_parts), parse_mode="Markdown")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text.replace("/remind", "").strip()

    if not text:
        await update.message.reply_text(
            "⏰ Set reminder time:\n\n"
            "`/remind 7:00` — every day at 7 AM\n"
            "`/remind off` — turn off",
            parse_mode="Markdown"
        )
        return

    if text.lower() == "off":
        jobs = context.job_queue.get_jobs_by_name("morning_reminder")
        for job in jobs:
            job.schedule_removal()
        await update.message.reply_text("⏰ Reminder turned off.")
        return

    try:
        hour, minute = map(int, text.replace(":", " ").split())
    except ValueError:
        await update.message.reply_text("Format: `/remind 7:00`", parse_mode="Markdown")
        return

    # Remove old reminders
    jobs = context.job_queue.get_jobs_by_name("morning_reminder")
    for job in jobs:
        job.schedule_removal()

    # Schedule new one (adjust for timezone)
    utc_hour = (hour - TIMEZONE_OFFSET) % 24
    reminder_time = dt_time(hour=utc_hour, minute=minute)

    context.job_queue.run_daily(
        morning_reminder,
        time=reminder_time,
        chat_id=update.effective_chat.id,
        name="morning_reminder"
    )

    await update.message.reply_text(f"✅ Reminder set for {hour}:{minute:02d} every day.")


async def morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Send morning reminder with Oura data."""
    oura_data = get_oura_summary()
    text = (
        f"☀️ Good morning! Time for your check-in.\n\n"
        f"{oura_data}\n\n"
        f"Tap /checkin or just tell me how you're feeling."
    )
    await context.bot.send_message(chat_id=context.job.chat_id, text=text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages — transcribe and treat as check-in."""
    if not is_authorized(update):
        return

    if not openai or not VOICE_API_KEY:
        await update.message.reply_text(
            "🎙 Voice messages require a Whisper API key. "
            "Add VOICE_API_KEY to .env (OpenAI, Groq, or xAI) or type your message instead."
        )
        return

    await update.message.reply_text("🎙 Transcribing voice message...")

    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    file_path = f"/tmp/voice_{update.message.message_id}.ogg"
    await file.download_to_drive(file_path)

    transcript = transcribe_voice(file_path)

    # Clean up
    try:
        os.remove(file_path)
    except OSError:
        pass

    if not transcript:
        await update.message.reply_text("❌ Could not transcribe. Try again or type your message instead.")
        return

    await update.message.reply_text(f"📝 Transcript: _{transcript}_", parse_mode="Markdown")

    # Add Oura data and send to Claude
    oura_data = get_oura_summary()
    prompt = f"{oura_data}\n\nMy voice check-in: {transcript}\n\nGive me today's workout."
    reply = ask_claude(prompt)
    await send_voice_reply(update, update.effective_chat.id, reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-form text messages."""
    if not is_authorized(update):
        return

    text = update.message.text
    reply = ask_claude(text)
    await send_voice_reply(update, update.effective_chat.id, reply)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /health command from Apple Shortcuts with HealthKit data."""
    if not is_authorized(update):
        return

    text = update.message.text
    apple_data = text.replace("/health", "").strip()

    if not apple_data:
        await update.message.reply_text(
            "⌚ This command is for automatic data from Apple Shortcuts.\n"
            "Set up the shortcut using the guide — it will send data here automatically."
        )
        return

    # Save Apple Watch data for today's check-in
    today = datetime.now().strftime("%Y-%m-%d")
    db_execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"apple_health_{today}", apple_data)
    )

    await update.message.reply_text(
        f"✅ Apple Watch data received!\n\n{apple_data}\n\n"
        "Data will be included in your next /checkin."
    )


def get_apple_health_today():
    """Retrieve today's Apple Watch data if available."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db_execute(
        "SELECT value FROM settings WHERE key = ?",
        (f"apple_health_{today}",), fetch=True
    )
    return rows[0][0] if rows else None


async def cmd_deload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check or reset the deload cycle."""
    if not is_authorized(update):
        return

    text = update.message.text.replace("/deload", "").strip()

    if text.lower() == "reset":
        week = reset_training_week()
        await update.message.reply_text(
            f"🔄 Cycle reset. Current week: {week}/4\n"
            "Deload will be on week 4."
        )
        return

    week, start = get_current_training_week()
    deload_status = " ⚠️ DELOAD!" if week == 4 else ""
    await update.message.reply_text(
        f"📅 *Training Cycle:*\n\n"
        f"Current week: *{week}/4*{deload_status}\n"
        f"Cycle start: {start}\n\n"
        f"Deload on week 4 — reduced volume and intensity.\n"
        f"Reset: `/deload reset`",
        parse_mode="Markdown"
    )


async def cmd_updateprogress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store a progression snapshot.

    Format: /updateprogress squat:65kg3x8 deadlift:85kg4x5 bench:50kg3x8
    """
    if not is_authorized(update):
        return

    text = update.message.text.replace("/updateprogress", "").strip()

    if not text:
        await update.message.reply_text(
            "📊 *Record Your Lifts*\n\n"
            "Format:\n"
            "`/updateprogress squat:65kg3x8 deadlift:85kg4x5 bench:50kg3x8 ohp:30kg3x8 hipthrust:80kg3x10 row:40kg3x10`\n\n"
            "For Pilates:\n"
            "`/updateprogress pilates:full_teaser,single_leg_stretch`\n\n"
            "You can update just the ones that changed.",
            parse_mode="Markdown"
        )
        return

    # Parse key:value pairs
    data = {}
    for pair in text.split():
        if ":" in pair:
            key, val = pair.split(":", 1)
            data[key.lower().strip()] = val.strip()

    if not data:
        await update.message.reply_text("❌ Could not parse. Format: `squat:65kg3x8`", parse_mode="Markdown")
        return

    save_progression_snapshot(data)

    saved = ", ".join(f"{k}: {v}" for k, v in data.items())
    await update.message.reply_text(f"✅ Lifts recorded!\n{saved}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏋️ *Commands:*\n\n"
        "📋 /checkin — morning check-in\n"
        "📅 /week — weekly training plan\n"
        "💪 /log — log a workout\n"
        "📈 /progress — progress with trends\n"
        "⏰ /remind — set reminder\n"
        "⌚ /health — Apple Watch data (from Shortcuts)\n"
        "🔄 /deload — check/reset training cycle\n"
        "📊 /updateprogress — record current lifts\n"
        "❓ /help — this help\n\n"
        "Or just text/voice anything — I'll respond as your coach."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Scheduled Jobs ────────────────────────────────────────────

async def sunday_weekly_plan(context: ContextTypes.DEFAULT_TYPE):
    """Auto-send weekly plan on Sundays."""
    # Advance training week
    new_week = advance_training_week()
    logger.info(f"Training week advanced to {new_week}/4")

    plan = generate_weekly_plan()
    text = f"📅 *Auto Weekly Plan* (week {new_week}/4)\n\n{plan}"
    await send_voice_reply(context, context.job.chat_id, text)


async def biweekly_progression_checkin(context: ContextTypes.DEFAULT_TYPE):
    """Every 14 days, ask user to update progression numbers."""
    text = (
        "📊 *Time to update your lifts!*\n\n"
        "It's been 2 weeks — record your current working weights:\n\n"
        "`/updateprogress squat:XXkg deadlift:XXkg bench:XXkg ohp:XXkg hipthrust:XXkg row:XXkg`\n\n"
        "Pilates milestones:\n"
        "`/updateprogress pilates:your_milestones_here`\n\n"
        "This helps track progress and adjust your program."
    )
    await context.bot.send_message(chat_id=context.job.chat_id, text=text, parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────
def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("deload", cmd_deload))
    app.add_handler(CommandHandler("updateprogress", cmd_updateprogress))

    # Callbacks (inline buttons)
    app.add_handler(CallbackQueryHandler(handle_checkin_callback))

    # Voice messages
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    # Free-form text (must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ── Scheduled jobs (only if ALLOWED_USER_ID is set) ──
    if ALLOWED_USER_ID:
        # Sunday weekly plan at 9 AM local time
        sunday_utc_hour = (9 - TIMEZONE_OFFSET) % 24
        app.job_queue.run_daily(
            sunday_weekly_plan,
            time=dt_time(hour=sunday_utc_hour, minute=0),
            days=(6,),  # Sunday = 6
            chat_id=int(ALLOWED_USER_ID),
            name="sunday_weekly_plan"
        )

        # Biweekly progression check-in (every 14 days, Monday 10 AM local)
        monday_utc_hour = (10 - TIMEZONE_OFFSET) % 24
        app.job_queue.run_repeating(
            biweekly_progression_checkin,
            interval=timedelta(days=14),
            first=dt_time(hour=monday_utc_hour, minute=0),
            chat_id=int(ALLOWED_USER_ID),
            name="biweekly_progression"
        )

    logger.info("🏋️ Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
