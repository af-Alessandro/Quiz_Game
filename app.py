import io
import json
import math
import os
import socket
import time
from pathlib import Path
import qrcode
from flask import Flask, render_template, send_file, request
from flask_socketio import SocketIO, emit, join_room
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TIME_LIMIT = 35
question_default_time = DEFAULT_TIME_LIMIT
MAX_POINTS = 1000
MIN_CORRECT_POINTS = 500
ASSET_VERSION = os.environ.get("RENDER_GIT_COMMIT", str(int(time.time())))[:12]

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chiave_segreta!')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
socketio = SocketIO(app, cors_allowed_origins="*")

# Caricamento delle domande dal file esterno JSON
def load_questions():
    global question_default_time

    try:
        with open(BASE_DIR / 'questions.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("Attenzione: File 'questions.json' non trovato!")
        return []

    if isinstance(data, dict):
        question_default_time = int(data.get("default_time", DEFAULT_TIME_LIMIT))
        return data.get("questions", [])

    question_default_time = DEFAULT_TIME_LIMIT
    return data

questions = load_questions()

# Stato del gioco in memoria
game_data = {
    "pin": "1234",
    "players": {},  # {player_id: {"name": str, "score": int, "sid": str}}
    "current_question": 0,
    "question_active": False,
    "question_started_at": None,
    "answers": {},
    "phase": "lobby",
    "last_results": None,
    "last_personal_results": {}
}

def get_join_url():
    return f"{get_public_base_url()}/player?pin={game_data['pin']}"

# Funzione per recuperare l'IP locale (utilizzata come fallback in locale)
def get_local_ip():
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        if s:
            s.close()
    return IP

def get_public_base_url():
    public_url = os.environ.get("PUBLIC_URL")
    if public_url:
        return public_url.rstrip("/")

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url.rstrip("/")

    base_url = request.host_url.rstrip("/")

    # In locale prova a generare un QR raggiungibile dal telefono sulla stessa Wi-Fi.
    if "localhost" in request.host or "127.0.0.1" in request.host:
        local_ip = get_local_ip()
        if local_ip != "127.0.0.1":
            return f"http://{local_ip}:5000"

    return base_url

def get_question_time_limit(question):
    return int(question.get("time", question_default_time))

def normalize_correct_index(question):
    correct = question.get("correct")
    options = question.get("options", [])

    if isinstance(correct, int):
        return correct if 0 <= correct < len(options) else None

    if isinstance(correct, str):
        value = correct.strip()
        if value.isdigit():
            index = int(value)
            return index if 0 <= index < len(options) else None

        for index, option in enumerate(options):
            if value.casefold() == str(option).strip().casefold():
                return index

    return None

def public_players():
    return [
        {"name": p["name"], "score": p["score"]}
        for p in game_data["players"].values()
    ]

def leaderboard():
    return sorted(public_players(), key=lambda x: x["score"], reverse=True)

def player_names():
    return [p["name"] for p in game_data["players"].values()]

def player_id_for_sid(sid):
    for player_id, player in game_data["players"].items():
        if player.get("sid") == sid:
            return player_id
    return None

def question_payload(q_index=None):
    if q_index is None:
        q_index = game_data["current_question"]
    if q_index >= len(questions):
        return None

    q = questions[q_index]
    time_limit = get_question_time_limit(q)
    remaining = time_limit
    if game_data["question_started_at"] is not None:
        elapsed = max(0, time.monotonic() - game_data["question_started_at"])
        remaining = max(0, math.ceil(time_limit - elapsed))

    return {
        "question": q["question"],
        "options": q["options"],
        "time": time_limit,
        "remaining": remaining,
        "number": q_index + 1,
        "total": len(questions)
    }

def emit_current_state(player_id=None):
    state = {
        "phase": game_data["phase"],
        "players": player_names(),
        "leaderboard": leaderboard(),
        "current_question": game_data["current_question"],
        "total": len(questions)
    }

    if game_data["phase"] == "question":
        state["question"] = question_payload()
        if player_id:
            state["answered"] = player_id in game_data["answers"]
    elif game_data["phase"] == "results":
        state["results"] = game_data["last_results"]
        if player_id:
            state["player_result"] = game_data["last_personal_results"].get(player_id)
    elif game_data["phase"] == "game_over":
        state["leaderboard"] = leaderboard()

    emit("game_state", state)

def current_results():
    q_index = game_data["current_question"]
    if q_index >= len(questions):
        return None

    q = questions[q_index]
    correct_index = normalize_correct_index(q)
    if correct_index is None:
        print(f"Domanda {q_index + 1}: valore 'correct' non valido: {q.get('correct')!r}")
        return None

    correct_text = q["options"][correct_index]
    answers_by_player = game_data["answers"]

    players = []
    personal_results = {}
    for player_id, player in game_data["players"].items():
        answer_data = answers_by_player.get(player_id, {})
        answer = answer_data.get("answer")
        is_correct = answer == correct_index
        points = answer_data.get("points", 0) if is_correct else 0
        player["score"] += points

        players.append({
            "name": player["name"],
            "score": player["score"],
            "answer": answer,
            "correct": is_correct,
            "points": points
        })
        personal_results[player_id] = {
            "answer": answer,
            "correct": is_correct,
            "points": points,
            "score": player["score"],
            "correct_answer": correct_index,
            "correct_text": correct_text
        }

    return {
        "leaderboard": sorted(players, key=lambda x: x["score"], reverse=True),
        "correct": correct_index,
        "correct_text": correct_text,
        "personal_results": personal_results
    }

@app.route('/host')
def host():
    return render_template(
        'host.html',
        pin=game_data["pin"],
        join_url=get_join_url(),
        asset_version=ASSET_VERSION
    )

@app.route('/')
@app.route('/player')
def player():
    return render_template(
        'player.html',
        pin=request.args.get("pin", ""),
        asset_version=ASSET_VERSION
    )

# Rotta per la generazione dinamica del QR Code (Locale o Cloud Render)
@app.route('/qrcode')
def get_qrcode():
    url = get_join_url()
    
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    response = send_file(buf, mimetype='image/png')
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@app.route('/health')
def health():
    return {"status": "ok"}

# WebSocket: Connessione Giocatore
@socketio.on('join_game')
def handle_join(data):
    pin = data.get('pin')
    name = (data.get('name') or "").strip()
    player_id = (data.get('player_id') or request.sid).strip()
    
    if pin != game_data["pin"]:
        emit('join_error', {"message": "PIN Errato"})
        return

    if not name:
        emit('join_error', {"message": "Inserisci il nome"})
        return

    join_room(pin)
    existing_score = game_data["players"].get(player_id, {}).get("score", 0)
    game_data["players"][player_id] = {
        "name": name,
        "score": existing_score,
        "sid": request.sid
    }

    emit('player_joined', {"players": player_names()}, to=pin)
    emit('join_success', {"status": "ok", "player_id": player_id, "name": name})
    emit_current_state(player_id)

@socketio.on('join_host')
def handle_join_host(data):
    pin = data.get('pin')
    if pin != game_data["pin"]:
        emit('join_error', {"message": "PIN Errato"})
        return

    join_room(pin)
    emit_current_state()

# WebSocket: Avvio Domanda
@socketio.on('start_next_question')
def handle_next_question():
    q_index = game_data["current_question"]
    if q_index < len(questions):
        game_data["question_active"] = True
        game_data["question_started_at"] = time.monotonic()
        game_data["answers"] = {}
        game_data["phase"] = "question"
        game_data["last_results"] = None
        game_data["last_personal_results"] = {}

        emit('new_question', question_payload(q_index), to=game_data["pin"])
    else:
        game_data["phase"] = "game_over"
        emit('game_over', {"leaderboard": leaderboard()}, to=game_data["pin"])

@socketio.on('reset_game')
def handle_reset_game():
    game_data["players"] = {}
    game_data["current_question"] = 0
    game_data["question_active"] = False
    game_data["question_started_at"] = None
    game_data["answers"] = {}
    game_data["phase"] = "lobby"
    game_data["last_results"] = None
    game_data["last_personal_results"] = {}

    emit('game_reset', {"players": [], "clear_players": True}, to=game_data["pin"])

# WebSocket: Invio Risposta
@socketio.on('submit_answer')
def handle_answer(data):
    answer_idx = data.get('answer')
    q_index = game_data["current_question"]
    player_id = player_id_for_sid(request.sid)

    try:
        answer_idx = int(answer_idx)
    except (TypeError, ValueError):
        return
    
    if (
        not game_data["question_active"]
        or q_index >= len(questions)
        or player_id not in game_data["players"]
        or player_id in game_data["answers"]
    ):
        return

    q = questions[q_index]
    if answer_idx < 0 or answer_idx >= len(q.get("options", [])):
        return

    time_limit = get_question_time_limit(q)
    elapsed = 0
    if game_data["question_started_at"] is not None:
        elapsed = max(0, time.monotonic() - game_data["question_started_at"])

    correct_index = normalize_correct_index(q)
    if correct_index is None:
        print(f"Domanda {q_index + 1}: valore 'correct' non valido: {q.get('correct')!r}")
        return

    is_correct = answer_idx == correct_index
    remaining_ratio = max(0, (time_limit - elapsed) / time_limit)
    points = 0
    if is_correct:
        speed_bonus = int((MAX_POINTS - MIN_CORRECT_POINTS) * remaining_ratio)
        points = MIN_CORRECT_POINTS + speed_bonus

    game_data["answers"][player_id] = {
        "answer": answer_idx,
        "correct": is_correct,
        "points": points
    }

# WebSocket: Fine Domanda e Classifica
@socketio.on('end_question')
def handle_end_question():
    if not game_data["question_active"]:
        return

    results = current_results()
    game_data["question_active"] = False
    game_data["current_question"] += 1

    if results:
        personal_results = results.pop("personal_results", {})
        game_data["phase"] = "results"
        game_data["last_results"] = results
        game_data["last_personal_results"] = personal_results
        emit('question_results', results, to=game_data["pin"])
        for player_id, personal_result in personal_results.items():
            sid = game_data["players"].get(player_id, {}).get("sid")
            if sid:
                emit('player_result', personal_result, to=sid)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)
