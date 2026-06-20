# app.py - QuestForge Local → powered by official Grok xAI API (.env version)
from flask import Flask, render_template, request, jsonify, redirect, session
import sqlite3
import uuid
import json
import os
import random
import re
import requests
from dotenv import load_dotenv   # ← This line is new
DB_FILE = "campaigns.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        history TEXT,
        character TEXT
    )
    """)

    conn.commit()
    conn.close()

init_db()

def load_rules():
    try:
        with open("dm_rules.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print("Warning: dm_rules.txt not found")
        return ""

# Load .env from the same directory as app.py
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "speak_friend_and_enter")
APP_PASSWORD = os.getenv("APP_PASSWORD")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')

        if password == APP_PASSWORD:
            session['authenticated'] = True
            return redirect('/')

        return render_template(
            'login.html',
            error='Incorrect password'
        )

    return render_template('login.html')
@app.route('/')
def index():
    if not session.get('authenticated'):
        return redirect('/login')

    return render_template('index.html')
    
def get_campaign_id():
    if "campaign_id" not in session:
        session["campaign_id"] = str(uuid.uuid4())
    return session["campaign_id"]
    
SAVE_FILE = "campaign_save.json"

# These will now come from .env (with sensible defaults/fallbacks)
XAI_API_KEY = os.getenv("XAI_API_KEY")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4")   # default to grok-4 if not specified

# Graceful error if key is missing
if not XAI_API_KEY or XAI_API_KEY.strip() == "" or "your-real-api-key" in XAI_API_KEY:
    print("\n⚠️  ERROR: xAI API key not found!")
    print("   Create a .env file in this folder with:")
    print("   XAI_API_KEY=xai-yourActualKeyHere\n")
    exit(1)

RULES_TEXT = load_rules()

SYSTEM_PROMPT = f"""
You are QuestForge.

You must obey the rules and instructions below at all times.

{RULES_TEXT}
"""
Header format (show at top of every response after game starts):
=== QUESTFORGE ===
System: D&D 5e (or whatever chosen)
Location: The Misty Forest | Day 12 - Dawn
HP: 32/32 | AC: 17 | Spell Slots: 4/4 1st, 3/3 2nd
Gold: 247 | Active Quest: Slay the Frost Giant Jarl

Dice format (you decide when to roll):
[Rolling 1d20 + 5 Stealth → 19] → You melt into the shadows!

Player commands you MUST recognize:
 /inv → full inventory
 /sheet → full character sheet
 /roll 2d6+3 → manual roll
 /map → ASCII/current area map
 /save → confirm save
 /rest → short/long rest
 /meta → out-of-character talk

Be vivid, funny when appropriate, ruthless when needed. Reward genius, punish stupidity — fairly."""

def load_game():
    campaign_id = get_campaign_id()
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT history FROM campaigns WHERE id = ?", (campaign_id,))
    row = c.fetchone()

    if row:
        return json.loads(row["history"])

    return {
        "history": [{"role": "system", "content": SYSTEM_PROMPT}]
    }


def save_game(game):
    campaign_id = get_campaign_id()
    conn = get_db()
    c = conn.cursor()

    history_json = json.dumps(game)

    c.execute("""
    INSERT INTO campaigns (id, history)
    VALUES (?, ?)
    ON CONFLICT(id) DO UPDATE SET history=excluded.history
    """, (campaign_id, history_json))

    conn.commit()
    conn.close()

game = None

def roll_dice(dice: str):
    dice = dice.strip().lower().replace(" ", "")
    match = re.match(r'(\d*)d(\d+)([+-]?\d*)', dice)
    if not match:
        return f"Invalid dice: {dice}"
    num = int(match.group(1)) if match.group(1) else 1
    sides = int(match.group(2))
    mod = int(match.group(3) or 0)
    rolls = [random.randint(1, sides) for _ in range(num)]
    total = sum(rolls) + mod
    detail = " + ".join(map(str, rolls))
    if mod != 0:
        detail += f" {'+' if mod > 0 else ''}{mod}"
    return f"🎲 {dice.upper()} → {detail} = **{total}**"

def build_campaign_context(game):
    char = game["character"]

    return f"""
CURRENT CHARACTER

Name: {char['name']}
Level: {char['level']}
HP: {char['hp']}/{char['max_hp']}
AC: {char['ac']}
Gold: {char['gold']}

Inventory:
{", ".join(char['inventory']) if char['inventory'] else "Empty"}

Conditions:
{", ".join(char['conditions']) if char['conditions'] else "None"}
"""

@app.route('/chat', methods=['POST'])
def chat():
    global game

    if game is None:
        game = load_game()

    user_message = request.json['message'].strip()

    # Local manual roll command (faster + real randomness)
    if user_message.lower().startswith('/roll '):
        result = roll_dice(user_message[6:])
        game["history"].append({"role": "assistant", "content": result})
        save_game(game)
        return jsonify({"response": result})

    # Let Grok handle the rest
    game["history"].append({"role": "user", "content": user_message})

    campaign_context = build_campaign_context(game)

messages = [
    {
        "role": "system",
        "content": campaign_context
    }
] + game["history"]

payload = {
    "model": GROK_MODEL,
    "messages": messages,
    "temperature": 0.85,
    "max_tokens": 4096
}

    try:
        response = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            json=payload,
            timeout=90
        )

        response.raise_for_status()
        ai_text = response.json()["choices"][0]["message"]["content"]

        # Replace any [Rolling ...] placeholders with real rolls
        for placeholder in re.findall(r'\[Rolling ([^\]]+?)\]', ai_text):
            real = roll_dice(placeholder)
            ai_text = ai_text.replace(f"[Rolling {placeholder}]", real)

        game["history"].append({"role": "assistant", "content": ai_text})
        save_game(game)

        return jsonify({"response": ai_text})

    except requests.exceptions.RequestException as e:
        return jsonify({"response": f"⚠️ Connection/API error: {str(e)}"})

    except Exception as e:
        return jsonify({"response": f"⚠️ Unexpected error: {str(e)}"})

    # Let Grok handle the rest
    game["history"].append({"role": "user", "content": user_message})

    payload = {
        "model": GROK_MODEL,
        "messages": game["history"],
        "temperature": 0.85,
        "max_tokens": 4096
    }

    try:
        response = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            json=payload,
            timeout=90
        )
        response.raise_for_status()
        ai_text = response.json()["choices"][0]["message"]["content"]

        # Replace any [Rolling ...] placeholders with real rolls
        for placeholder in re.findall(r'\[Rolling ([^\]]+?)\]', ai_text):
            real = roll_dice(placeholder)
            ai_text = ai_text.replace(f"[Rolling {placeholder}]", real)

        game["history"].append({"role": "assistant", "content": ai_text})
        save_game(game)

    except requests.exceptions.RequestException as e:
        ai_text = f"⚠️ Connection/API error: {str(e)}"
    except Exception as e:
        ai_text = f"⚠️ Unexpected error: {str(e)}"

    return jsonify({"response": ai_text})

if __name__ == '__main__':
    print("🚀 QuestForge Local (Grok xAI API) → http://localhost:5000")
    print(f"   Model: {GROK_MODEL}")
    print("   Loaded rules: rules/dm_rules.txt")
    if __name__ == '__main__':
        import os
        port = int(os.environ.get("PORT", 5000))
        app.run(host='0.0.0.0', port=port)
