from flask import Flask, render_template, request
from flask_socketio import SocketIO, send, emit
import os
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

# 🔥 Render 대응 + WebSocket 안정
socketio = SocketIO(app, cors_allowed_origins="*")

users = {}          # sid -> nickname
spectators = set()  # 관전자
roles = {}          # sid -> role
alive = set()       # 살아있는 sid
votes = {}          # 투표

phase = "waiting"

mafia_target = None
doctor_target = None
police_target = None


@app.route('/')
def index():
    return render_template('index.html')


# ------------------------
# 💀 죽은 사람 리스트
# ------------------------
def send_dead_list():
    dead = [
        f"{users[s]} ({roles[s]})"
        for s in users
        if s not in alive
    ]
    socketio.emit('dead_list', dead)


# ------------------------
# 👥 입장
# ------------------------
@socketio.on('join')
def handle_join(username):
    username = (username or "").strip()

    # 👁️ 관전자
    if username == "":
        spectators.add(request.sid)
        emit('join_success', to=request.sid)
        emit('spectator', True, to=request.sid)
        return

    # ❌ 중복 닉네임
    if username in users.values():
        emit('error', '이미 사용중인 닉네임입니다!', to=request.sid)
        return

    users[request.sid] = username
    emit('join_success', to=request.sid)

    socketio.emit('user_list', list(users.values()))
    send(f"👤 {username} 입장", broadcast=True)


# ------------------------
# 🎮 게임 시작
# ------------------------
@socketio.on('start_game')
def start_game():
    global roles, alive, phase

    if len(users) < 4:
        emit('error', '최소 4명 필요!', to=request.sid)
        return

    sids = list(users.keys())

    role_list = ["마피아", "의사", "경찰"] + ["시민"] * (len(sids) - 3)
    random.shuffle(role_list)

    roles.clear()
    alive.clear()
    votes.clear()

    for sid, role in zip(sids, role_list):
        roles[sid] = role
        alive.add(sid)
        emit('role', role, to=sid)

    # 🕵️ 마피아끼리 알림
    mafia = [s for s in sids if roles[s] == "마피아"]
    mafia_names = [users[s] for s in mafia]

    for s in mafia:
        others = [n for n in mafia_names if n != users[s]]
        if others:
            emit('message', f"🕵️ 동료 마피아: {', '.join(others)}", to=s)

    # 👁️ 관전자 전체 공개
    for s in spectators:
        emit('all_roles', {users[k]: roles[k] for k in users}, to=s)

    send_dead_list()

    phase = "night"
    socketio.emit('phase', 'night')
    socketio.emit('game_started')
    send("🌙 밤이 되었습니다...", broadcast=True)


# ------------------------
# 💬 채팅
# ------------------------
@socketio.on('message')
def handle_message(msg):
    sid = request.sid

    if sid in spectators:
        return

    if sid in alive:
        send(f"{users[sid]}: {msg}", broadcast=True)
    else:
        # 👻 죽은 사람 채팅 (죽은 사람끼리만)
        for s in users:
            if s not in alive:
                send(f"👻 {users[sid]}: {msg}", to=s)


# ------------------------
# 🌙 밤 행동
# ------------------------
@socketio.on('night_action')
def night_action(target_name):
    global mafia_target, doctor_target, police_target

    if request.sid not in alive or phase != "night":
        return

    role = roles.get(request.sid)

    target_sid = next((s for s, n in users.items() if n == target_name), None)
    if not target_sid or target_sid not in alive:
        return

    if role == "마피아":
        mafia_target = target_sid
        send("🕵️ 마피아가 누군가를 선택했습니다...", broadcast=True)

    elif role == "의사":
        doctor_target = target_sid
        send("💉 의사가 치료 대상을 선택했습니다...", broadcast=True)

    elif role == "경찰":
        police_target = target_sid
        send("🚓 경찰이 조사 중입니다...", broadcast=True)

        if roles[target_sid] == "마피아":
            emit('message', f"🔎 {users[target_sid]} → 마피아입니다!", to=request.sid)
        else:
            emit('message', f"🔎 {users[target_sid]} → 시민입니다.", to=request.sid)

    # 종료 조건
    mafia_done = not any(roles[s] == "마피아" and s in alive for s in roles) or mafia_target
    doctor_done = not any(roles[s] == "의사" and s in alive for s in roles) or doctor_target
    police_done = not any(roles[s] == "경찰" and s in alive for s in roles) or police_target

    if mafia_done and doctor_done and police_done:
        end_night()


def end_night():
    global phase, mafia_target, doctor_target, police_target

    if mafia_target and mafia_target != doctor_target:
        alive.discard(mafia_target)
        send(f"💀 {users[mafia_target]} 사망", broadcast=True)
    else:
        send("✨ 아무도 죽지 않았습니다.", broadcast=True)

    mafia_target = None
    doctor_target = None
    police_target = None

    send_dead_list()
    check_win()

    if phase == "end":
        return

    phase = "day"
    votes.clear()

    socketio.emit('phase', 'day')
    send("☀️ 낮이 되었습니다.", broadcast=True)


# ------------------------
# 🗳️ 투표
# ------------------------
@socketio.on('vote')
def vote(target_name):
    if request.sid not in alive or phase != "day":
        return

    if request.sid in votes:
        emit('error', '이미 투표했습니다!', to=request.sid)
        return

    target_sid = next((s for s, n in users.items() if n == target_name), None)
    if not target_sid or target_sid not in alive:
        return

    votes[request.sid] = target_sid
    send(f"🗳️ {users[request.sid]} 투표 완료", broadcast=True)

    if len(votes) == len(alive):
        end_day()


def end_day():
    global phase

    count = {}
    for t in votes.values():
        count[t] = count.get(t, 0) + 1

    if count:
        max_votes = max(count.values())
        top = [s for s, c in count.items() if c == max_votes]

        if len(top) == 1:
            alive.discard(top[0])
            send(f"⚰️ {users[top[0]]} 처형되었습니다.", broadcast=True)
        else:
            send("⚖️ 동률 → 아무도 처형되지 않았습니다.", broadcast=True)

    send_dead_list()
    check_win()

    if phase == "end":
        return

    phase = "night"
    socketio.emit('phase', 'night')
    send("🌙 다시 밤이 되었습니다...", broadcast=True)


# ------------------------
# 🏆 승리 조건
# ------------------------
def check_win():
    global phase

    mafia = sum(1 for s in alive if roles[s] == "마피아")
    citizen = len(alive) - mafia

    if mafia == 0:
        send("🎉 시민 승리!", broadcast=True)
        reveal_roles()
        phase = "end"

    elif mafia >= citizen:
        send("💀 마피아 승리!", broadcast=True)
        reveal_roles()
        phase = "end"


def reveal_roles():
    send("📢 전체 직업 공개", broadcast=True)
    for s in users:
        send(f"{users[s]} : {roles[s]}", broadcast=True)


# ------------------------
# ❌ 접속 종료
# ------------------------
@socketio.on('disconnect')
def disconnect():
    users.pop(request.sid, None)
    roles.pop(request.sid, None)
    alive.discard(request.sid)
    spectators.discard(request.sid)

    socketio.emit('user_list', list(users.values()))


# ------------------------
# 🚀 실행 (Render 대응)
# ------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)