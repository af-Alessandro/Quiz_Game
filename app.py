import io
import json
import os
import socket
from pathlib import Path
import qrcode
from flask import Flask, render_template, send_file, request
from flask_socketio import SocketIO, emit, join_room
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent

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
    "current_question": 0
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
        emit('new_question', {
            "question": q["question"],
            "options": q["options"]
        }, to=game_data["pin"])
    else:
        leaderboard = sorted(game_data["players"].values(), key=lambda x: x["score"], reverse=True)
        leaderboard = [p for p in leaderboard if p["name"] != 'HOST']
        emit('game_over', {"leaderboard": leaderboard}, to=game_data["pin"])

@socketio.on('reset_game')
def handle_reset_game():
    game_data["current_question"] = 0
    for player in game_data["players"].values():
        player["score"] = 0

    player_names = [p["name"] for p in game_data["players"].values() if p["name"] != 'HOST']
    emit('game_reset', {"players": player_names}, to=game_data["pin"])

# WebSocket: Invio Risposta
@socketio.on('submit_answer')
def handle_answer(data):
    answer_idx = data.get('answer')
    q_index = game_data["current_question"]
    
    if q_index < len(questions):
        if answer_idx == questions[q_index]["correct"]:
            if request.sid in game_data["players"]:
                game_data["players"][request.sid]["score"] += 100

# WebSocket: Fine Domanda e Classifica
@socketio.on('end_question')
def handle_end_question():
    game_data["current_question"] += 1
    leaderboard = sorted(game_data["players"].values(), key=lambda x: x["score"], reverse=True)
    leaderboard = [p for p in leaderboard if p["name"] != 'HOST']
    emit('question_results', {"leaderboard": leaderboard}, to=game_data["pin"])

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)
