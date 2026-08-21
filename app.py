import io
import json
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
MAX_POINTS = 1000
MIN_CORRECT_POINTS = 500

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chiave_segreta!')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
socketio = SocketIO(app, cors_allowed_origins="*")

# Caricamento delle domande dal file esterno JSON
def load_questions():
    try:
        with open(BASE_DIR / 'questions.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("Attenzione: File 'questions.json' non trovato!")
        return []

questions = load_questions()

# Stato del gioco in memoria
game_data = {
    "pin": "1234",
    "players": {},  # {socket_id: {"name": str, "score": int}}
    "current_question": 0,
    "question_active": False,
    "question_started_at": None,
    "answers": {}
}

def get_join_url():
    return f"{get_public_base_url()}/?pin={game_data['pin']}"

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
    return int(question.get("time", DEFAULT_TIME_LIMIT))

def public_players():
    return [
        {"name": p["name"], "score": p["score"]}
        for p in game_data["players"].values()
        if p["name"] != 'HOST'
    ]

def leaderboard():
    return sorted(public_players(), key=lambda x: x["score"], reverse=True)

def current_results():
    q_index = game_data["current_question"]
    if q_index >= len(questions):
        return None

    q = questions[q_index]
    correct_index = q["correct"]
    correct_text = q["options"][correct_index]
    answers_by_sid = game_data["answers"]

    players = []
    for sid, player in game_data["players"].items():
        if player["name"] == 'HOST':
            continue

        answer_data = answers_by_sid.get(sid, {})
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

    return {
        "leaderboard": sorted(players, key=lambda x: x["score"], reverse=True),
        "correct": correct_index,
        "correct_text": correct_text
    }

@app.route('/host')
def host():
    return render_template('host.html', pin=game_data["pin"], join_url=get_join_url())

@app.route('/')
def player():
    return render_template('player.html', pin=request.args.get("pin", ""))

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

# WebSocket: Connessione Giocatore
@socketio.on('join_game')
def handle_join(data):
    pin = data.get('pin')
    name = data.get('name')
    
    if pin == game_data["pin"]:
        join_room(pin)
        game_data["players"][request.sid] = {"name": name, "score": 0}
        
        player_names = [p["name"] for p in game_data["players"].values() if p["name"] != 'HOST']
        emit('player_joined', {"players": player_names}, to=pin)
        emit('join_success', {"status": "ok"})
    else:
        emit('join_error', {"message": "PIN Errato"})

# WebSocket: Avvio Domanda
@socketio.on('start_next_question')
def handle_next_question():
    q_index = game_data["current_question"]
    if q_index < len(questions):
        q = questions[q_index]
        time_limit = get_question_time_limit(q)
        game_data["question_active"] = True
        game_data["question_started_at"] = time.monotonic()
        game_data["answers"] = {}

        emit('new_question', {
            "question": q["question"],
            "options": q["options"],
            "time": time_limit,
            "number": q_index + 1,
            "total": len(questions)
        }, to=game_data["pin"])
    else:
        emit('game_over', {"leaderboard": leaderboard()}, to=game_data["pin"])

@socketio.on('reset_game')
def handle_reset_game():
    game_data["current_question"] = 0
    game_data["question_active"] = False
    game_data["question_started_at"] = None
    game_data["answers"] = {}
    for player in game_data["players"].values():
        player["score"] = 0

    player_names = [p["name"] for p in game_data["players"].values() if p["name"] != 'HOST']
    emit('game_reset', {"players": player_names}, to=game_data["pin"])

# WebSocket: Invio Risposta
@socketio.on('submit_answer')
def handle_answer(data):
    answer_idx = data.get('answer')
    q_index = game_data["current_question"]
    
    if (
        not game_data["question_active"]
        or q_index >= len(questions)
        or request.sid not in game_data["players"]
        or request.sid in game_data["answers"]
    ):
        return

    q = questions[q_index]
    time_limit = get_question_time_limit(q)
    elapsed = 0
    if game_data["question_started_at"] is not None:
        elapsed = max(0, time.monotonic() - game_data["question_started_at"])

    is_correct = answer_idx == q["correct"]
    remaining_ratio = max(0, (time_limit - elapsed) / time_limit)
    points = 0
    if is_correct:
        speed_bonus = int((MAX_POINTS - MIN_CORRECT_POINTS) * remaining_ratio)
        points = MIN_CORRECT_POINTS + speed_bonus

    game_data["answers"][request.sid] = {
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
        emit('question_results', results, to=game_data["pin"])

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)
