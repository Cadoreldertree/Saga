# app.py - QuestForge Local → Grok + SQLite persistent RPG

from flask import Flask, render_template, request, jsonify, redirect, session
import sqlite3
import uuid
import json
import os
import random
import re
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "speak_friend_and_enter")

# ---------------- DATABASE ----------------

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

# ---------------- SESSION CAMPAIGN ID ----------------

def get_campaign_id():
    if "campaign_id" not in session:
        session["campaign_id"] = str(uuid.uuid4())
    return session["campaign_id"]

# ---------------- ENV ----------------

XAI_API_KEY = os.getenv("XAI_API_KEY")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4")
APP_PASSWORD = os.getenv("APP_PASSWORD")

if not XAI_API_KEY:
    print("Missing XAI API key")
    exit(1)

# ---------------- GAME DATA ----------------

SYSTEM_PROMPT = """You are QuestForge — a living Dungeon Master.
Track HP, inventory, gold, quests, combat, and narrative continuity."""

DEFAULT_CHARACTER = {
    "name": "Unnamed Hero",
    "level": 1,
    "hp": 10,
    "max_hp": 10,
    "ac": 10,
    "gold": 0,
    "inventory": [],
    "conditions": []
}

# ---------------- LOAD / SAVE ----------------

def load_game():
    campaign_id = get_campaign_id()
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT history, character FROM campaigns WHERE id = ?", (campaign_id,))
    row = c.fetchone()

    if row:
        return {
            "history": json.loads(row["history"]),
            "character": json.loads(row["character"])
        }

    return {
        "history": [{"role": "system", "content": SYSTEM_PROMPT}],
        "character": DEFAULT_CHARACTER.copy()
    }


def save_game(game):
    campaign_id = get_campaign_id()
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    INSERT INTO campaigns (id, history, character)
    VALUES (?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        history=excluded.history,
        character=excluded.character
    """, (
        campaign_id,
        json.dumps(game["history"]),
        json.dumps(game["character"])
    ))

    conn.commit()
    conn.close()

# ---------------- CHARACTER SYSTEM ----------------

def update_character(game, text):
    char = game["character"]

    # gold gain
    for g in re.findall(r"\+(\d+) gold", text):
        char["gold"] += int(g)

    # items
    for item in re.findall(r"obtained ([a-zA-Z0-9 ]+)", text):
        char["inventory"].append(item.strip())

    # healing
    for h in re.findall(r"heal(?:ed)? (\d+)", text):
        char["hp"] = min(char["max_hp"], char["hp"] + int(h))

    # damage
    for d in re.findall(r"takes (\d+) damage", text):
        char["hp"] = max(0, char["hp"] - int(d))

# ---------------- ROLL SYSTEM ----------------

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
    if mod:
        detail += f" {'+' if mod > 0 else ''}{mod}"

    return f"🎲 {dice.upper()} → {detail} = **{total}**"

# ---------------- ROUTES ----------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect("/")
        return render_template("login.html", error="Wrong password")

    return render_template("login.html")


@app.route('/')
def index():
    if not session.get("authenticated"):
        return redirect("/login")
    return render_template("index.html")


@app.route('/chat', methods=['POST'])
def chat():
    global game

    if game is None:
        game = load_game()

    user_message = request.json['message'].strip()
    lower = user_message.lower()

    # ---------------- COMMANDS ----------------

    if lower.startswith("/roll "):
        result = roll_dice(user_message[6:])
        game["history"].append({"role": "assistant", "content": result})
        save_game(game)
        return jsonify({"response": result})

    if lower == "/inv":
        return jsonify({"response": f"🎒 Inventory: {game['character']['inventory']}"})

    if lower == "/sheet":
        c = game["character"]
        return jsonify({
            "response": (
                f"📜 Character Sheet\n"
                f"Name: {c['name']}\n"
                f"HP: {c['hp']}/{c['max_hp']}\n"
                f"AC: {c['ac']}\n"
                f"Gold: {c['gold']}\n"
                f"Inventory: {c['inventory']}\n"
            )
        })

    # ---------------- AI PIPELINE ----------------

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

        # dice replacement
        for placeholder in re.findall(r'\[Rolling ([^\]]+?)\]', ai_text):
            ai_text = ai_text.replace(
                f"[Rolling {placeholder}]",
                roll_dice(placeholder)
            )

        # update character
        update_character(game, ai_text)

        game["history"].append({"role": "assistant", "content": ai_text})
        save_game(game)

        return jsonify({"response": ai_text})

    except Exception as e:
        return jsonify({"response": f"⚠️ Error: {str(e)}"})


# ---------------- RUN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)