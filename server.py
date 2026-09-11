from datetime import datetime, timezone
import base64
import binascii
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import time
from io import BytesIO
from queue import Empty, Full, Queue
from tempfile import NamedTemporaryFile
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit

from PIL import Image, UnidentifiedImageError

# 預設綁定所有網卡，讓同一區域網內的多位使用者都能連上這台伺服器。
# 只想本機使用時可設定 COLLABNOTE_HOST=127.0.0.1。
HOST = os.environ.get("COLLABNOTE_HOST", "0.0.0.0")
PORT = int(os.environ.get("COLLABNOTE_PORT", sys.argv[1] if len(sys.argv) > 1 else 8765))
DATA_FILE = Path(__file__).with_name("notes.json")
USERS_FILE = Path(__file__).with_name("users.json")
AVATAR_DIR = Path(__file__).with_name("frontend") / "assets" / "avatars"
IMAGE_DIR = Path(__file__).with_name("frontend") / "assets" / "images"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DATA_LOCK = Lock()
SESSIONS = {}
PRESENCE = {}
PRESENCE_LOCK = Lock()
PRESENCE_TIMEOUT = 12.0
# 即時事件（Server-Sent Events）：每個已連線的前端一個佇列
EVENT_LOCK = Lock()
EVENT_SUBSCRIBERS = {}
EVENT_QUEUE_SIZE = 64
EVENT_HEARTBEAT = 15.0
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")


def load_notes():
    if not DATA_FILE.exists():
        save_notes([])
    try:
        notes = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return notes if isinstance(notes, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_notes(notes):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=DATA_FILE.parent, delete=False) as temp_file:
        json.dump(notes, temp_file, ensure_ascii=False, indent=2)
        temp_file.write("\n")
        temp_path = Path(temp_file.name)
    os.replace(temp_path, DATA_FILE)


def load_users():
    if not USERS_FILE.exists():
        save_users([])
    try:
        users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return users if isinstance(users, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_users(users):
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=USERS_FILE.parent, delete=False) as temp_file:
        json.dump(users, temp_file, ensure_ascii=False, indent=2)
        temp_file.write("\n")
        temp_path = Path(temp_file.name)
    os.replace(temp_path, USERS_FILE)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex()
    return salt, password_hash


def authenticate_user(username, password):
    for user in load_users():
        if user["username"] != username:
            continue
        _, password_hash = hash_password(password, user["salt"])
        if hmac.compare_digest(password_hash, user["passwordHash"]):
            return user
        return None
    return None


def create_session(user):
    token = secrets.token_urlsafe(32)
    # sid 用來辨識「同一個登入」的來源，前端可忽略自己造成的事件
    SESSIONS[token] = {"username": user["username"], "sid": secrets.token_hex(8)}
    return token


def session_public(session):
    return {"sid": session.get("sid", "")}


def find_user(username):
    return next((user for user in load_users() if user["username"] == username), None)


def is_user_online(username):
    return any(session["username"] == username for session in SESSIONS.values())


def presence_people_for(note_id, username):
    """Live roster for a note, limited to the current user and their friends."""
    with PRESENCE_LOCK:
        now = time.monotonic()
        for token, presence in list(PRESENCE.items()):
            if token not in SESSIONS or now - presence["ts"] > PRESENCE_TIMEOUT:
                PRESENCE.pop(token, None)
        user = find_user(username)
        if not user:
            return []
        friend_names = set(user.get("friends", []))
        latest_by_user = {}
        for presence in PRESENCE.values():
            if presence.get("noteId") != note_id:
                continue
            if presence["username"] != username and presence["username"] not in friend_names:
                continue
            seen = latest_by_user.get(presence["username"])
            if seen is None or presence["ts"] >= seen["ts"]:
                latest_by_user[presence["username"]] = presence
        people = []
        for presence in latest_by_user.values():
            person = find_user(presence["username"])
            if not person:
                continue
            people.append({**public_user(person), "mode": presence["mode"]})
        return people


def publish_event(event):
    """把事件送給所有即時連線；個別佇列滿了就丟棄，客戶端重連時會重新載入。"""
    with EVENT_LOCK:
        subscribers = list(EVENT_SUBSCRIBERS.values())
    for subscriber in subscribers:
        try:
            subscriber["queue"].put_nowait(event)
        except Full:
            pass


def publish_presence(note_id):
    """在線名單依觀看者而不同，因此對每位訂閱者分別計算。"""
    if note_id is None:
        return
    with EVENT_LOCK:
        subscribers = list(EVENT_SUBSCRIBERS.items())
    for client_id, subscriber in subscribers:
        people = presence_people_for(note_id, subscriber["username"])
        try:
            subscriber["queue"].put_nowait({"type": "presence", "noteId": note_id, "people": people})
        except Full:
            pass


def public_user(user):
    return {
        "username": user["username"],
        "displayName": user.get("displayName", user["username"]),
        "title": user.get("title", "团队成员"),
        "avatarUrl": f"/api/users/{user['username']}/avatar" if user.get("avatarFile") else None,
    }


class BadPayload(Exception):
    """請求內容不是合法的 JSON 物件；錯誤回應已於 read_payload 內送出。"""


class NotesHandler(BaseHTTPRequestHandler):
    # SSE 需要長連線，因此改用 HTTP/1.1；其他回應一律帶 Connection: close 維持舊行為
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        # 長時間的 SSE 連線靠 TCP keepalive 偵測消失的客戶端
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

    def handle_one_request(self):
        # 單一客戶端送出損壞的內容時，不要讓請求執行緒直接中斷而讓前端一直等待回應
        try:
            super().handle_one_request()
        except BadPayload:
            self.close_connection = True

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/health":
            self.send_json(200, {"ok": True})
        elif self.path == "/api/auth/me":
            user = self.current_user()
            if user:
                self.send_json(200, {**session_public(user), **public_user(user)})
            else:
                self.send_json(401, {"error": "Authentication required"})
        elif urlsplit(self.path).path == "/api/events":
            self.stream_events()
        elif self.path == "/api/friends":
            self.list_friends()
        elif urlsplit(self.path).path.startswith("/api/images/"):
            self.send_note_image()
        elif urlsplit(self.path).path.startswith("/api/users/") and urlsplit(self.path).path.endswith("/avatar"):
            self.send_avatar()
        elif self.path == "/api/notes" and self.current_user():
            with DATA_LOCK:
                self.send_json(200, load_notes())
        elif self.path == "/api/notes":
            self.send_json(401, {"error": "Authentication required"})
        else:
            self.send_json(404, {"error": "Not found"})

    def stream_events(self):
        """Server-Sent Events：把筆記與在線狀態的變更即時推給所有前端。"""
        parsed = urlsplit(self.path)
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            # EventSource 無法自訂標頭，因此允許以 query string 傳遞 token
            token = (parse_qs(parsed.query).get("token") or [""])[0].strip()
        session = SESSIONS.get(token)
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return

        subscriber = {
            "username": session["username"],
            "sid": session.get("sid", ""),
            "queue": Queue(maxsize=EVENT_QUEUE_SIZE),
        }
        client_id = secrets.token_hex(8)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with EVENT_LOCK:
            EVENT_SUBSCRIBERS[client_id] = subscriber
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = subscriber["queue"].get(timeout=EVENT_HEARTBEAT)
                except Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                line = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with EVENT_LOCK:
                EVENT_SUBSCRIBERS.pop(client_id, None)

    def do_POST(self):
        if self.path == "/api/auth/register":
            self.register_user()
            return
        if self.path == "/api/auth/login":
            self.login_user()
            return
        if self.path == "/api/auth/logout":
            self.logout_user()
            return
        if self.path == "/api/auth/profile":
            self.update_profile()
            return
        if self.path == "/api/images/upload":
            self.upload_note_image()
            return
        if self.path == "/api/presence":
            self.post_presence()
            return
        if self.path == "/api/friends":
            self.request_friend()
            return
        if self.path.startswith("/api/friends/requests/"):
            self.handle_friend_request()
            return
        if self.path != "/api/notes":
            self.send_json(404, {"error": "Not found"})
            return
        if not self.current_user():
            self.send_json(401, {"error": "Authentication required"})
            return
        payload = self.read_payload()
        with DATA_LOCK:
            notes = load_notes()
            note = {
                "id": int(datetime.now(timezone.utc).timestamp() * 1000),
                "title": payload.get("title", "未命名筆記"),
                "tag": "草稿",
                "tagStyle": "background:#f1f5f9;color:#475569;",
                "content": payload.get("content", "開始撰寫你的筆記…"),
                "time": "剛剛",
                "editors": ["green"],
            }
            notes.insert(0, note)
            save_notes(notes)
        publish_event({"type": "note-created", "note": note, "sid": self.current_session_sid()})
        self.send_json(201, note)

    def do_PUT(self):
        if not self.path.startswith("/api/notes/"):
            self.send_json(404, {"error": "Not found"})
            return
        if not self.current_user():
            self.send_json(401, {"error": "Authentication required"})
            return
        try:
            note_id = int(self.path.rsplit("/", 1)[1])
        except ValueError:
            self.send_json(400, {"error": "Invalid note id"})
            return
        payload = self.read_payload()
        with DATA_LOCK:
            notes = load_notes()
            for note in notes:
                if note["id"] == note_id:
                    note.update({key: payload[key] for key in ("title", "content") if key in payload})
                    save_notes(notes)
                    publish_event({"type": "note-updated", "note": note, "sid": self.current_session_sid()})
                    self.send_json(200, note)
                    return
        self.send_json(404, {"error": "Note not found"})

    def do_DELETE(self):
        if not self.path.startswith("/api/notes/"):
            self.send_json(404, {"error": "Not found"})
            return
        if not self.current_user():
            self.send_json(401, {"error": "Authentication required"})
            return
        try:
            note_id = int(self.path.rsplit("/", 1)[1])
        except ValueError:
            self.send_json(400, {"error": "Invalid note id"})
            return

        with DATA_LOCK:
            notes = load_notes()
            remaining_notes = [note for note in notes if note["id"] != note_id]
            if len(remaining_notes) == len(notes):
                self.send_json(404, {"error": "Note not found"})
                return
            save_notes(remaining_notes)
        publish_event({"type": "note-deleted", "noteId": note_id, "sid": self.current_session_sid()})
        self.send_json(200, {"deleted": note_id})

    def read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            self.close_connection = True
            self.send_json(400, {"error": "请求内容必须是合法的 JSON 物件"})
            raise BadPayload()
        return payload

    def post_presence(self):
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        payload = self.read_payload()
        note_id = payload.get("noteId")
        mode = "editing" if payload.get("mode") == "editing" else "viewing"
        if note_id is None:
            with PRESENCE_LOCK:
                previous = PRESENCE.pop(token, None)
            if previous and previous.get("noteId") is not None:
                publish_presence(previous["noteId"])
            self.send_json(200, {"noteId": None, "people": []})
            return
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Invalid note id"})
            return
        with PRESENCE_LOCK:
            PRESENCE[token] = {
                "username": session["username"],
                "noteId": note_id,
                "mode": mode,
                "ts": time.monotonic(),
            }
        people = presence_people_for(note_id, session["username"])
        publish_presence(note_id)
        self.send_json(200, {"noteId": note_id, "people": people})

    def current_session_sid(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        session = SESSIONS.get(token)
        return session.get("sid", "") if session else ""

    def current_user(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        return SESSIONS.get(token)

    def current_user_record(self):
        session = self.current_user()
        return find_user(session["username"]) if session else None

    def list_friends(self):
        user = self.current_user_record()
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return
        def friend_info(username):
            friend = find_user(username)
            if not friend:
                return None
            return {**public_user(friend), "online": is_user_online(username)}

        friends = [info for username in user.get("friends", []) if (info := friend_info(username))]
        incoming = [
            info for username in user.get("friendRequests", []) if (info := friend_info(username))
        ]
        outgoing = []
        for candidate in load_users():
            if user["username"] in candidate.get("friendRequests", []):
                info = friend_info(candidate["username"])
                if info:
                    outgoing.append(info)
        self.send_json(200, {"friends": friends, "incomingRequests": incoming, "outgoingRequests": outgoing})

    def request_friend(self):
        user = self.current_user_record()
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return
        username = str(self.read_payload().get("username", "")).strip()
        if not USERNAME_PATTERN.fullmatch(username):
            self.send_json(400, {"error": "请输入有效的登录名"})
            return
        if username == user["username"]:
            self.send_json(400, {"error": "不能添加自己"})
            return
        with DATA_LOCK:
            users = load_users()
            friend = next((item for item in users if item["username"] == username), None)
            if not friend:
                self.send_json(404, {"error": "找不到该用户"})
                return
            current = next(item for item in users if item["username"] == user["username"])
            if username in current.get("friends", []):
                self.send_json(409, {"error": "对方已经是好友"})
                return
            if user["username"] in friend.get("friendRequests", []):
                self.send_json(409, {"error": "好友请求已发送"})
                return
            friend.setdefault("friendRequests", []).append(user["username"])
            save_users(users)
        self.send_json(201, {"requested": username})

    def handle_friend_request(self):
        user = self.current_user_record()
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return
        parts = self.path.split("/")
        if len(parts) != 6 or parts[5] not in ("accept", "reject"):
            self.send_json(400, {"error": "Invalid friend request"})
            return
        requester_name = unquote(parts[4])
        action = parts[5]
        with DATA_LOCK:
            users = load_users()
            current = next((item for item in users if item["username"] == user["username"]), None)
            requester = next((item for item in users if item["username"] == requester_name), None)
            if not current or not requester_name in current.get("friendRequests", []) or not requester:
                self.send_json(404, {"error": "好友请求不存在"})
                return
            current["friendRequests"].remove(requester_name)
            if action == "accept":
                current.setdefault("friends", [])
                requester.setdefault("friends", [])
                if requester_name not in current["friends"]:
                    current["friends"].append(requester_name)
                if user["username"] not in requester["friends"]:
                    requester["friends"].append(user["username"])
            save_users(users)
        self.send_json(200, {"accepted": action == "accept", "username": requester_name})

    def register_user(self):
        payload = self.read_payload()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if not USERNAME_PATTERN.fullmatch(username):
            self.send_json(400, {"error": "用户名需为 3-32 位字母、数字、下划线或中文"})
            return
        if len(password) < 6:
            self.send_json(400, {"error": "密码至少需要 6 位"})
            return
        with DATA_LOCK:
            users = load_users()
            if any(user["username"] == username for user in users):
                self.send_json(409, {"error": "用户名已存在"})
                return
            salt, password_hash = hash_password(password)
            users.append({"username": username, "salt": salt, "passwordHash": password_hash})
            save_users(users)
            user = users[-1]
        token = create_session(user)
        self.send_json(201, {"token": token, **session_public(SESSIONS[token]), **public_user(user)})

    def login_user(self):
        payload = self.read_payload()
        user = authenticate_user(str(payload.get("username", "")).strip(), str(payload.get("password", "")))
        if not user:
            self.send_json(401, {"error": "用户名或密码错误"})
            return
        token = create_session(user)
        self.send_json(200, {"token": token, **session_public(SESSIONS[token]), **public_user(user)})

    def logout_user(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        SESSIONS.pop(token, None)
        with PRESENCE_LOCK:
            previous = PRESENCE.pop(token, None)
        if previous and previous.get("noteId") is not None:
            publish_presence(previous["noteId"])
        self.send_json(200, {"ok": True})

    def update_profile(self):
        user = self.current_user()
        if not user:
            self.send_json(401, {"error": "Authentication required"})
            return
        payload = self.read_payload()
        display_name = str(payload.get("displayName", user.get("displayName", user["username"]))).strip()
        title = str(payload.get("title", user.get("title", "团队成员"))).strip()
        avatar_data = payload.get("avatarData")
        if not 1 <= len(display_name) <= 32:
            self.send_json(400, {"error": "昵称长度需为 1-32 个字符"})
            return
        if len(title) > 40:
            self.send_json(400, {"error": "头衔不能超过 40 个字符"})

        avatar_file = user.get("avatarFile")
        if avatar_data:
            try:
                header, encoded = avatar_data.split(",", 1)
                if header not in ("data:image/jpeg;base64", "data:image/png;base64"):
                    raise ValueError
                image_bytes = base64.b64decode(encoded, validate=True)
                if len(image_bytes) > 8 * 1024 * 1024:
                    raise ValueError
                with Image.open(BytesIO(image_bytes)) as image:
                    if image.format not in ("JPEG", "PNG"):
                        raise ValueError
                    image = image.convert("RGB")
                    side = min(image.size)
                    left = (image.width - side) // 2
                    top = (image.height - side) // 2
                    image = image.crop((left, top, left + side, top + side))
                    image = image.resize((256, 256), Image.Resampling.LANCZOS)
                    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
                    avatar_file = f"{secrets.token_hex(12)}.jpg"
                    image.save(AVATAR_DIR / avatar_file, "JPEG", quality=85, optimize=True)
            except (ValueError, OSError, UnidentifiedImageError, binascii.Error):
                self.send_json(400, {"error": "头像必须是有效的 JPG 或 PNG 图片"})
                return

        with DATA_LOCK:
            users = load_users()
            for stored_user in users:
                if stored_user["username"] == user["username"]:
                    stored_user["displayName"] = display_name
                    stored_user["title"] = title or "团队成员"
                    if avatar_file:
                        stored_user["avatarFile"] = avatar_file
                    save_users(users)
                    self.send_json(200, public_user(stored_user))
                    return
        self.send_json(404, {"error": "User not found"})

    def send_avatar(self):
        avatar_path = urlsplit(self.path).path
        username = unquote(avatar_path.split("/")[3])
        for user in load_users():
            if user["username"] == username and user.get("avatarFile"):
                avatar_path = AVATAR_DIR / user["avatarFile"]
                if avatar_path.is_file():
                    body = avatar_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    self.wfile.write(body)
                    return
        self.send_json(404, {"error": "Avatar not found"})

    def upload_note_image(self):
        if not self.current_user():
            self.send_json(401, {"error": "Authentication required"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("image/jpeg", "image/png"):
            self.send_json(400, {"error": "仅支持 JPG 或 PNG 图片"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Invalid Content-Length"})
            return
        if length <= 0 or length > MAX_IMAGE_BYTES:
            self.send_json(413, {"error": "图片为空或超过 10MB 上限"})
            return
        raw = self.rfile.read(length)
        try:
            with Image.open(BytesIO(raw)) as image:
                if image.format not in ("JPEG", "PNG"):
                    raise ValueError
        except (ValueError, OSError, UnidentifiedImageError):
            self.send_json(400, {"error": "图片数据无效"})
            return
        extension = ".jpg" if content_type == "image/jpeg" else ".png"
        filename = f"{secrets.token_hex(16)}{extension}"
        try:
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile("wb", dir=IMAGE_DIR, delete=False) as temp_file:
                temp_file.write(raw)
                temp_path = Path(temp_file.name)
            os.replace(temp_path, IMAGE_DIR / filename)
        except OSError:
            self.send_json(500, {"error": "圖片存儲失敗"})
            return
        self.send_json(201, {"url": f"/api/images/{filename}"})

    def send_note_image(self):
        image_path = unquote(urlsplit(self.path).path)
        filename = image_path.removeprefix("/api/images/")
        if not filename or filename != Path(filename).name or ".." in filename:
            self.send_json(404, {"error": "Not found"})
            return
        image_file = IMAGE_DIR / filename
        if not image_file.is_file():
            self.send_json(404, {"error": "Image not found"})
            return
        extension = image_file.suffix.lower()
        content_type = "image/jpeg" if extension in (".jpg", ".jpeg") else "image/png" if extension == ".png" else "application/octet-stream"
        body = image_file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def lan_addresses():
    """回傳本機可用於區域網連線的 IPv4 位址。"""
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if not address.startswith("127."):
                addresses.add(address)
        finally:
            probe.close()
    except OSError:
        pass
    return sorted(addresses)


def print_banner():
    print(f"Notes API listening on http://{HOST}:{PORT}", flush=True)
    if HOST in ("0.0.0.0", "::"):
        for address in lan_addresses():
            print(f"  區域網連線地址: http://{address}:{PORT}", flush=True)
        print("  多位使用者可將前端伺服器地址設為上述任一網址後登入不同帳號。", flush=True)
    print("  本機連線地址: http://127.0.0.1:%d" % PORT, flush=True)


class NotesServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 多個前端同時連線（含 SSE 長連線）時避免連線佇列過短
    request_queue_size = 64


if __name__ == "__main__":
    server = NotesServer((HOST, PORT), NotesHandler)
    print_banner()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
