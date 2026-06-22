from flask import Flask, request, jsonify
import psycopg2
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import re
import datetime

app = Flask(__name__)

# ── Config ── FILL THESE IN ───────────────────────────────
GEMINI_API_KEY = "AQ.Ab8RN6KXZJ1V4jO0qTuGbexHEfCwkjrn8Ko_JLPhkSjKxIn99Q"
POSTGRES_PASSWORD = "msn@2006"
SHEET_ID = "1fY7gUeHOxbtVcFJsxg4lkYSw_twjPo8m9NNQbAKoS-U"
CREDENTIALS_FILE = "certials.json" 
# ── Gemini ────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ── Google Sheets ─────────────────────────────────────────
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
gc    = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID)

def get_or_create_tab(name, headers):
    try:
        return sheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=name, rows=200, cols=10)
        ws.append_row(headers)
        return ws

# ── PostgreSQL ────────────────────────────────────────────
def get_db():
    return psycopg2.connect(
        host="localhost", database="postgres",
        port=5432, user="postgres",
        password=POSTGRES_PASSWORD
    )

# ── Gemini: parse commit message ──────────────────────────
def parse_commit(message):
    prompt = f"""
You are a commit message parser for a productivity OS called GitOps for Humans.
Parse this git commit message and return ONLY valid JSON, nothing else.

Commit: "{message}"

Rules:
- starts with "task:"       → {{"type":"task","title":"<extracted>"}}
- starts with "meeting:"    → {{"type":"meeting","summary":"<extracted>"}}
- starts with "expense:"    → {{"type":"expense","description":"<item>","amount":"<amount or unknown>"}}
- starts with "idea:"       → {{"type":"idea","content":"<extracted>"}}
- starts with "brainstorm:" → {{"type":"brainstorm","content":"<extracted>"}}
- starts with "bug:"        → {{"type":"bug","title":"<extracted>","severity":"<1-5 guess>"}}
- anything else             → {{"type":"unknown","raw":"{message}"}}

Return ONLY the JSON object. No explanation. No markdown. No code fences.
"""
    response = model.generate_content(prompt)
    raw = re.sub(r"^```json|^```|```$", "", response.text.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)

# ── Gemini: brainstorm roadmap ────────────────────────────
def generate_roadmap(topic):
    prompt = f"""
You are a startup advisor. Someone just had this idea: "{topic}"
Generate a JSON response with:
{{
  "title": "<idea title>",
  "requirements": ["req1", "req2", "req3"],
  "risks": ["risk1", "risk2"],
  "roadmap": ["Week 1: ...", "Week 2: ...", "Week 3: ..."]
}}
Return ONLY JSON. No markdown. No explanation.
"""
    response = model.generate_content(prompt)
    raw = re.sub(r"^```json|^```|```$", "", response.text.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)

# ── Route parsed commit → correct DB table + Sheets tab ───
def route_to_table(cur, parsed, author):
    t   = parsed.get("type")
    now = str(datetime.datetime.now())

    if t == "task":
        title = parsed.get("title")
        cur.execute("INSERT INTO tasks (title) VALUES (%s)", (title,))
        ws = get_or_create_tab("Tasks", ["Title", "Status", "Author", "Created At"])
        ws.append_row([title, "pending", author, now])
        print(f"  ✅ Task → Sheets: {title}")

    elif t == "meeting":
        summary = parsed.get("summary")
        cur.execute("INSERT INTO meetings (summary) VALUES (%s)", (summary,))
        ws = get_or_create_tab("Meetings", ["Summary", "Author", "Created At"])
        ws.append_row([summary, author, now])
        print(f"  📅 Meeting → Sheets: {summary}")

    elif t == "expense":
        desc   = parsed.get("description")
        amount = parsed.get("amount", "unknown")
        cur.execute("INSERT INTO expenses (description, amount) VALUES (%s,%s)", (desc, amount))
        ws = get_or_create_tab("Expenses", ["Description", "Amount", "Author", "Created At"])
        ws.append_row([desc, amount, author, now])
        print(f"  💸 Expense → Sheets: {desc} | {amount}")

    elif t == "idea":
        content = parsed.get("content")
        cur.execute("INSERT INTO ideas (content) VALUES (%s)", (content,))
        ws = get_or_create_tab("Ideas", ["Idea", "Author", "Created At"])
        ws.append_row([content, author, now])
        print(f"  💡 Idea → Sheets: {content}")

    elif t == "brainstorm":
        topic   = parsed.get("content")
        roadmap = generate_roadmap(topic)
        cur.execute("INSERT INTO ideas (content) VALUES (%s)", (f"BRAINSTORM: {topic}",))
        ws = get_or_create_tab("Brainstorms", ["Title", "Requirements", "Risks", "Roadmap", "Author", "Created At"])
        ws.append_row([
            roadmap.get("title"),
            ", ".join(roadmap.get("requirements", [])),
            ", ".join(roadmap.get("risks", [])),
            ", ".join(roadmap.get("roadmap", [])),
            author, now
        ])
        print(f"  🧠 Brainstorm → Sheets: {roadmap.get('title')}")

    elif t == "bug":
        title    = parsed.get("title")
        severity = parsed.get("severity", "unknown")
        cur.execute("INSERT INTO tasks (title) VALUES (%s)", (f"BUG [{severity}]: {title}",))
        ws = get_or_create_tab("Bugs", ["Title", "Severity", "Status", "Author", "Created At"])
        ws.append_row([title, severity, "open", author, now])
        print(f"  🐛 Bug → Sheets: [{severity}] {title}")

    else:
        print(f"  ❓ Unknown commit type: {parsed.get('raw')}")

# ── Webhook: Flask's ONLY job = catch bytes, insert row ───
@app.route("/webhook", methods=["POST"])
def webhook():
    data    = request.json
    commits = data.get("commits", [])

    conn = get_db()
    cur  = conn.cursor()

    for commit in commits:
        message = commit.get("message", "")
        author  = commit.get("author", {}).get("name", "unknown")
        print(f"\n📥 Commit from {author}: {message}")

        # ── Flask inserts raw commit — PostgreSQL trigger routes it ──
        cur.execute(
            "INSERT INTO commits (author, message, processed) VALUES (%s, %s, FALSE)",
            (author, message)
        )
        # Postgres trigger (process_commit) fires here automatically ^^

        # ── Gemini parses + pushes to Sheets ──
        try:
            parsed = parse_commit(message)
            print(f"  🤖 Gemini parsed: {parsed}")
            route_to_table(cur, parsed, author)
            cur.execute(
                "UPDATE commits SET processed = TRUE WHERE message = %s AND author = %s",
                (message, author)
            )
        except Exception as e:
            print(f"  ⚠️ Error: {e}")

    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "trigger fired, sheets updated"}), 200

@app.route("/", methods=["GET"])
def index():
    return "GitOps for Humans — PostgreSQL is the backend ✅"

if __name__ == "__main__":
    app.run(port=5000, debug=True)