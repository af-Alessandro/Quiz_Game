import io
import json
import socket
import qrcode
from flask import Flask, render_template, send_file, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'chiave_segreta!'
socketio = SocketIO(app, cors_allowed_origins="*")

# Caricamento delle domande dal file esterno JSON
def load_questions():
    try:
        with open('questions.json', 'r', encoding='utf-8') as f:
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

# Funzione per recuperare l'IP locale (utilizzata come fallback in locale)
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

@app.route('/host')
def host():
    return render_template('host.html')

@app.route('/')
def player():
    return render_template('player.html')

# Rotta per la generazione dinamica del QR Code (Locale o Cloud Render)
@app.route('/qrcode')
def get_qrcode():
    # Rileva automaticamente se il sito è in HTTPS (come su Render) o HTTP
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
    host = request.host
    
    # Costruisce l'URL base (es. https://quiz-game.onrender.com)
    base_url = f"{scheme}://{host}"
    
    # Se stai testando sul tuo PC in locale, usa l'IP Wi-Fi per far connettere i telefoni
    if "localhost" in host or "127.0.0.1" in host:
        base_url = f"http://{get_local_ip()}:5000"

    url = f"{base_url}/?pin={game_data['pin']}"
    
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

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
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)