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
import sys
from io import BytesIO
from tempfile import NamedTemporaryFile
from threading import Lock
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError

HOST = os.environ.get("COLLABNOTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("COLLABNOTE_PORT", sys.argv[1] if len(sys.argv) > 1 else 8765))
DATA_FILE = Path(__file__).with_name("notes.json")
USERS_FILE = Path(__file__).with_name("users.json")
AVATAR_DIR = Path(__file__).with_name("frontend") / "assets" / "avatars"
DATA_LOCK = Lock()
SESSIONS = {}
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
    SESSIONS[token] = {"username": user["username"]}
    return token


def find_user(username):
    return next((user for user in load_users() if user["username"] == username), None)


def is_user_online(username):
    return any(session["username"] == username for session in SESSIONS.values())


def public_user(user):
    return {
        "username": user["username"],
        "displayName": user.get("displayName", user["username"]),
        "title": user.get("title", "团队成员"),
        "avatarUrl": f"/api/users/{user['username']}/avatar" if user.get("avatarFile") else None,
    }


class NotesHandler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/health":
            self.send_json(200, {"ok": True})
        elif self.path == "/api/auth/me":
            user = self.current_user()
            if user:
                self.send_json(200, public_user(user))
            else:
                self.send_json(401, {"error": "Authentication required"})
        elif self.path == "/api/friends":
            self.list_friends()
        elif urlsplit(self.path).path.startswith("/api/users/") and urlsplit(self.path).path.endswith("/avatar"):
            self.send_avatar()
        elif self.path == "/api/notes" and self.current_user():
            with DATA_LOCK:
                self.send_json(200, load_notes())
        elif self.path == "/api/notes":
            self.send_json(401, {"error": "Authentication required"})
        else:
            self.send_json(404, {"error": "Not found"})

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
        self.send_json(200, {"deleted": note_id})

    def read_payload(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

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
        self.send_json(201, {"token": create_session(user), **public_user(user)})

    def login_user(self):
        payload = self.read_payload()
        user = authenticate_user(str(payload.get("username", "")).strip(), str(payload.get("password", "")))
        if not user:
            self.send_json(401, {"error": "用户名或密码错误"})
            return
        self.send_json(200, {"token": create_session(user), **public_user(user)})

    def logout_user(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        SESSIONS.pop(token, None)
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
                    self.end_headers()
                    self.wfile.write(body)
                    return
        self.send_json(404, {"error": "Avatar not found"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), NotesHandler)
    print(f"Notes API listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
