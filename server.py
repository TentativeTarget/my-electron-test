from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
from tempfile import NamedTemporaryFile
from threading import Lock

HOST = os.environ.get("COLLABNOTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("COLLABNOTE_PORT", sys.argv[1] if len(sys.argv) > 1 else 8765))
DATA_FILE = Path(__file__).with_name("notes.json")
USERS_FILE = Path(__file__).with_name("users.json")
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
        if self.path == "/api/auth/me":
            user = self.current_user()
            if user:
                self.send_json(200, {"username": user["username"]})
            else:
                self.send_json(401, {"error": "Authentication required"})
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
        self.send_json(201, {"token": create_session(user), "username": username})

    def login_user(self):
        payload = self.read_payload()
        user = authenticate_user(str(payload.get("username", "")).strip(), str(payload.get("password", "")))
        if not user:
            self.send_json(401, {"error": "用户名或密码错误"})
            return
        self.send_json(200, {"token": create_session(user), "username": user["username"]})

    def logout_user(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        SESSIONS.pop(token, None)
        self.send_json(200, {"ok": True})

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
