from collections import deque
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
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from io import BytesIO
from queue import Empty, Full, Queue
from tempfile import NamedTemporaryFile, gettempdir
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit

from PIL import Image, UnidentifiedImageError
from database import StorageConfigError, create_storage, LedgerStore, JsonStorage, JsonLedgerStore

try:
    import bleach
    from bleach.css_sanitizer import CSSSanitizer
except ImportError as error:
    raise RuntimeError("Missing backend dependencies; run: python3 -m pip install -r requirements.txt") from error

# 預設綁定所有網卡，讓同一區域網內的多位使用者都能連上這台伺服器。
# 只想本機使用時可設定 COLLABNOTE_HOST=127.0.0.1。
HOST = os.environ.get("COLLABNOTE_HOST", "0.0.0.0")
# 第一個參數是連接埠；--monitor 模式（見檔尾）不會用到，需避免被當成數字解析
PORT = int(os.environ.get("COLLABNOTE_PORT",
                          sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765))


def base_dir():
    """資料檔的基準目錄：打包成單一執行檔後改用執行檔所在目錄，才不會寫進解壓暫存區。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = base_dir()
DATA_FILE = BASE_DIR / "notes.json"
USERS_FILE = BASE_DIR / "users.json"
# 使用者上傳的檔案一律由後端保存，不放在前端目錄內
DATA_DIR = BASE_DIR / "data"
AVATAR_DIR = DATA_DIR / "avatars"
IMAGE_DIR = DATA_DIR / "images"
# 舊版把上傳檔寫在 frontend/assets/ 底下，開機時搬到 DATA_DIR
LEGACY_AVATAR_DIR = BASE_DIR / "frontend" / "assets" / "avatars"
LEGACY_IMAGE_DIR = BASE_DIR / "frontend" / "assets" / "images"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DATA_LOCK = Lock()
STORAGE = None
SESSIONS = {}
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 天无活动则失效
PRESENCE = {}
PRESENCE_LOCK = Lock()
PRESENCE_TIMEOUT = 12.0
# 即時事件（Server-Sent Events）：每個已連線的前端一個佇列
EVENT_LOCK = Lock()
EVENT_SUBSCRIBERS = {}
EVENT_QUEUE_SIZE = 64
EVENT_HEARTBEAT = 15.0
# 終端監控器用的伺服器日誌：環形緩衝 + SSE 訂閱者
LOG_BUFFER_SIZE = 500
LOG_SUBSCRIBER_QUEUE_SIZE = 256
LOG_LOCK = Lock()
LOG_BUFFER = deque(maxlen=LOG_BUFFER_SIZE)
LOG_SUBSCRIBERS = {}
# 設 COLLABNOTE_LOG_STDOUT=0 可關閉同步輸出到終端
LOG_STDOUT = os.environ.get("COLLABNOTE_LOG_STDOUT", "1") not in ("0", "", "false", "False")
# 非本機來源要讀監控端點時，必須帶 X-Monitor-Token 標頭
MONITOR_TOKEN = os.environ.get("COLLABNOTE_MONITOR_TOKEN", "")
# 後端啟動時是否自動開一個終端視窗跑 monitor.py（COLLABNOTE_MONITOR_WINDOW=0 可關閉）
MONITOR_WINDOW = os.environ.get("COLLABNOTE_MONITOR_WINDOW", "1") not in ("0", "", "false", "False")
MONITOR_WINDOW_TITLE = "CollabNote 監控"
SERVER_STARTED_AT = datetime.now(timezone.utc)
SERVER_STARTED_MONOTONIC = time.monotonic()
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")


def storage():
    global STORAGE
    if STORAGE is None:
        STORAGE = create_storage(BASE_DIR)
        if hasattr(STORAGE, "initialize"):
            STORAGE.initialize()
    return STORAGE


LEDGER_STORE = None


def ledger_store():
    global LEDGER_STORE
    if LEDGER_STORE is None:
        store = storage()
        # JSON 模式用 JsonLedgerStore，MySQL 用 LedgerStore；两者接口一致
        LEDGER_STORE = JsonLedgerStore(store.base_dir) if isinstance(store, JsonStorage) else LedgerStore(store)
    return LEDGER_STORE


# ─── 账本四档权限：owner / admin / editor / viewer ───
LEDGER_ROLE_RANK = {"owner": 3, "admin": 2, "editor": 1, "viewer": 0}


def ledger_can(role, action):
    """判断某角色能否做某操作。action: view / write / edit / invite / delete / manage"""
    if role not in LEDGER_ROLE_RANK:
        return False
    rank = LEDGER_ROLE_RANK[role]
    if action == "view":
        return True
    if action in ("write", "edit"):
        return rank >= 1        # editor 以上
    if action == "invite":
        return rank >= 2        # admin 以上
    if action in ("delete", "manage"):
        return rank >= 2        # admin 以上
    return False


def load_notes():
    return storage().load_notes()


def save_notes(notes):
    storage().save_notes(notes)


def load_users():
    return storage().load_users()


def save_users(users):
    storage().save_users(users)


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


def _purge_expired_sessions():
    now = time.monotonic()
    for token, session in list(SESSIONS.items()):
        if now - session.get("ts", now) > SESSION_TTL_SECONDS:
            SESSIONS.pop(token, None)


def get_session(token):
    """带过期检查的 session 读取，同时刷新活跃时间。"""
    session = SESSIONS.get(token)
    if not session:
        return None
    now = time.monotonic()
    if now - session.get("ts", now) > SESSION_TTL_SECONDS:
        SESSIONS.pop(token, None)
        return None
    session["ts"] = now
    return session


def create_session(user):
    token = secrets.token_urlsafe(32)
    # sid 用來辨識「同一個登入」的來源，前端可忽略自己造成的事件
    _purge_expired_sessions()
    SESSIONS[token] = {"username": user["username"], "sid": secrets.token_hex(8), "ts": time.monotonic()}
    return token


def session_public(session):
    return {"sid": session.get("sid", "")}


def find_user(username):
    return next((user for user in load_users() if user["username"] == username), None)


# 舊筆記沒有建立者欄位，開機時補上（見 migrate_note_permissions）
LEGACY_NOTE_OWNER = "YvLvn"
LEGACY_NOTE_MEMBERS = ("LimeSev",)


def note_access(note, username):
    """回傳使用者對這篇筆記的權限：owner / edit / view / None（看不到）。"""
    if not username:
        return None
    owner = note.get("owner")
    if not owner:
        # 尚未遷移的舊資料：視為擁有者，避免升級後突然不能編輯
        return "owner"
    if username == owner:
        return "owner"
    if username in (note.get("members") or []):
        return "edit"
    if username in (note.get("viewers") or []):
        return "view"
    return None


def note_can_edit(note, username):
    return note_access(note, username) in ("owner", "edit")


def note_visible_to(note, username):
    return note_access(note, username) is not None


ALLOWED_HTML_TAGS = [
    "a", "b", "blockquote", "br", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "i", "img", "li", "ol", "p", "pre", "s",
    "span", "strong", "table", "tbody", "td", "th", "thead", "tr",
    "u", "ul",
]
ALLOWED_HTML_ATTRS = {
    "*": ["class", "style"],
    "a": ["href", "title"],
    "img": ["src", "alt", "title", "width", "height"],
}
HTML_CSS_SANITIZER = CSSSanitizer(allowed_css_properties={"font-size", "width", "height", "text-align"})


# 純文字型內容（不含任何排版標籤）不應該原樣帶到前端 innerHTML：
# 使用者打的 `<div>` 這種文字若被當 HTML，切換筆記後就會變成看不懂的標籤源碼。
def contains_real_markup(value):
    return bool(re.search(r"<\s*/?\s*([a-z][a-z0-9]*)\b[^>]*>", str(value or ""), re.IGNORECASE))


def normalize_note_content(value):
    """保存用户内容：只做危险标签清洗，不改写用户打的普通文字。

    以前这里会对「不像标签的文字」自动转义，结果用户输入 <div> 时被
    改写成 &lt;div&gt;，反而把源码写进了笔记。现在保存端不碰纯文字。
    """
    text = str(value or "")
    if contains_real_markup(text):
        return sanitize_note_html(text)
    return text


def sanitize_note_html(value):
    return bleach.clean(
        str(value or ""),
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRS,
        protocols=["http", "https"],
        css_sanitizer=HTML_CSS_SANITIZER,
        strip=True,
    )


def public_note(note, username):
    """輸出筆記並附上目前使用者的權限，前端據此決定 UI。"""
    access = note_access(note, username) or "none"
    owner = note.get("owner") or ""
    owner_record = find_user(owner) if owner else None
    return {
        **note,
        "access": access,
        "canEdit": access in ("owner", "edit"),
        "canManage": access == "owner",
        "ownerName": (owner_record.get("displayName") or owner) if owner_record else owner,
    }


def migrate_note_permissions():
    """為沒有建立者的舊筆記補上建立者與預設協作名單（只做一次）。"""
    usernames = [user["username"] for user in load_users()]
    owner = LEGACY_NOTE_OWNER if LEGACY_NOTE_OWNER in usernames else (usernames[0] if usernames else "")
    if not owner:
        return
    with DATA_LOCK:
        notes = load_notes()
        changed = False
        for note in notes:
            if note.get("owner"):
                continue
            note["owner"] = owner
            note["members"] = [name for name in LEGACY_NOTE_MEMBERS if name in usernames and name != owner]
            changed = True
        if changed:
            save_notes(notes)


def migrate_legacy_uploads():
    """把舊版放在 frontend/assets/ 的頭像與圖片搬到後端的 DATA_DIR（只做一次）。"""
    for legacy_dir, target_dir in ((LEGACY_AVATAR_DIR, AVATAR_DIR), (LEGACY_IMAGE_DIR, IMAGE_DIR)):
        if not legacy_dir.is_dir():
            continue
        for source in sorted(legacy_dir.iterdir()):
            if not source.is_file():
                continue
            target = target_dir / source.name
            if target.exists():
                continue
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                log_event(f"搬移舊上傳檔 {source.name} → {target_dir.name}/", "WARN")
            except OSError as error:
                log_event(f"搬移 {source.name} 失敗：{error}", "WARN")
        # 搬完後清掉留下來的空目錄（frontend/assets 空了也一併移除）
        for empty_dir in (legacy_dir, legacy_dir.parent):
            try:
                empty_dir.rmdir()
            except OSError:
                pass


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


def subscribers_snapshot():
    with EVENT_LOCK:
        return list(EVENT_SUBSCRIBERS.values())


def put_event(subscriber, event):
    try:
        subscriber["queue"].put_nowait(event)
    except Full:
        pass


def publish_note_event(event_type, note, sid, extra_usernames=()):
    """送出含筆記內容的事件：只給看得到的人，權限欄位再依收件者各自計算。"""
    extra = set(extra_usernames)
    for subscriber in subscribers_snapshot():
        username = subscriber["username"]
        if username not in extra and not note_visible_to(note, username):
            continue
        put_event(subscriber, {"type": event_type, "note": public_note(note, username), "sid": sid})


def publish_note_change(event_type, note, sid, extra_usernames=()):
    """送出不含內容的筆記事件（刪除、名單變更），同樣只給有權限的人。"""
    extra = set(extra_usernames)
    for subscriber in subscribers_snapshot():
        username = subscriber["username"]
        if username not in extra and not note_visible_to(note, username):
            continue
        put_event(subscriber, {"type": event_type, "noteId": note["id"], "sid": sid})


def publish_to_users(event, usernames):
    """只通知指定使用者（例如被移出協作名單的人）。"""
    targets = set(usernames)
    for subscriber in subscribers_snapshot():
        if subscriber["username"] in targets:
            put_event(subscriber, event)


def publish_ledger_event(ledger_id, sid, event_type="ledger-changed", member_usernames=()):
    """賬本變更：只推給擁有存取權的成員（含剛被邀請/移除的人）。"""
    targets = set(member_usernames)
    if not targets:
        try:
            targets = {m["username"] for m in ledger_store().list_members(ledger_id)}
        except Exception:
            targets = set()
    event = {"type": event_type, "ledgerId": ledger_id, "sid": sid}
    for subscriber in subscribers_snapshot():
        if subscriber["username"] in targets:
            put_event(subscriber, event)


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


def log_event(text, level="INFO"):
    """寫一行伺服器日誌：存進環形緩衝、推給監控端，必要時同步輸出到終端。"""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "text": text,
    }
    with LOG_LOCK:
        LOG_BUFFER.append(entry)
        for queue in list(LOG_SUBSCRIBERS.values()):
            try:
                queue.put_nowait(entry)
            except Full:
                pass
    if LOG_STDOUT:
        print(f"[{entry['time']}] {level:<7} {text}", flush=True)


def notify_monitors_shutdown():
    """後端要關了：通知監控器立刻結束，它才知道可以收掉視窗。"""
    with LOG_LOCK:
        for queue in list(LOG_SUBSCRIBERS.values()):
            try:
                queue.put_nowait({"type": "shutdown"})
            except Full:
                pass


def monitor_snapshot():
    """整理終端監控器需要的狀態：連線使用者名單與伺服器資訊。"""
    now = time.monotonic()
    with PRESENCE_LOCK:
        for token, presence in list(PRESENCE.items()):
            if token not in SESSIONS or now - presence["ts"] > PRESENCE_TIMEOUT:
                PRESENCE.pop(token, None)
        presence_by_user = {}
        for presence in PRESENCE.values():
            seen = presence_by_user.get(presence["username"])
            if seen is None or presence["ts"] >= seen["ts"]:
                presence_by_user[presence["username"]] = presence
    with EVENT_LOCK:
        streams_by_user = {}
        for subscriber in EVENT_SUBSCRIBERS.values():
            username = subscriber["username"]
            streams_by_user[username] = streams_by_user.get(username, 0) + 1
    sessions_by_user = {}
    for session in SESSIONS.values():
        username = session["username"]
        sessions_by_user[username] = sessions_by_user.get(username, 0) + 1
    titles = {note["id"]: note.get("title", "") for note in load_notes()}
    records = {user["username"]: user for user in load_users()}
    people = []
    names = sorted(set(sessions_by_user) | set(presence_by_user), key=lambda name: (name not in presence_by_user, name))
    for username in names:
        presence = presence_by_user.get(username)
        record = records.get(username) or {}
        people.append({
            "username": username,
            "displayName": record.get("displayName") or username,
            "online": presence is not None,
            "mode": presence.get("mode") if presence else None,
            "noteId": presence.get("noteId") if presence else None,
            "noteTitle": titles.get(presence.get("noteId")) if presence else None,
            "idleSeconds": round(now - presence["ts"], 1) if presence else None,
            "sessions": sessions_by_user.get(username, 0),
            "streams": streams_by_user.get(username, 0),
        })
    return {
        "server": {
            "host": HOST,
            "port": PORT,
            "startedAt": SERVER_STARTED_AT.isoformat(),
            "uptimeSeconds": round(time.monotonic() - SERVER_STARTED_MONOTONIC, 1),
            "userCount": len(records),
            "noteCount": len(titles),
            "sessionCount": len(SESSIONS),
            "streamCount": len(EVENT_SUBSCRIBERS),
            "onlineCount": len(presence_by_user),
        },
        "users": people,
    }


def public_user(user):
    return {
        "username": user["username"],
        "displayName": user.get("displayName", user["username"]),
        "title": user.get("title", "团队成员"),
        "avatarUrl": f"/api/users/{user['username']}/avatar" if user.get("avatarFile") else None,
    }


LOGIN_ATTEMPTS = {}
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300  # 5 分钟内超过 10 次失败则暂锁


def login_attempts_guard(key):
    """返回 (allowed, retry_seconds)。"""
    now = time.monotonic()
    record = LOGIN_ATTEMPTS.get(key)
    if not record:
        return True, 0
    count, first = record
    if now - first > LOGIN_WINDOW_SECONDS:
        LOGIN_ATTEMPTS.pop(key, None)
        return True, 0
    if count >= LOGIN_MAX_ATTEMPTS:
        return False, int(LOGIN_WINDOW_SECONDS - (now - first))
    return True, 0


def record_login_failure(key):
    now = time.monotonic()
    record = LOGIN_ATTEMPTS.get(key)
    if not record or now - record[1] > LOGIN_WINDOW_SECONDS:
        LOGIN_ATTEMPTS[key] = [1, now]
    else:
        record[0] += 1


def clear_login_failures(key):
    LOGIN_ATTEMPTS.pop(key, None)


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
            session = self.current_user()
            record = find_user(session["username"]) if session else None
            if record:
                self.send_json(200, {**session_public(session), **public_user(record)})
            else:
                self.send_json(401, {"error": "Authentication required"})
        elif urlsplit(self.path).path == "/api/events":
            self.stream_events()
        elif urlsplit(self.path).path == "/api/monitor/status":
            self.monitor_status()
        elif urlsplit(self.path).path == "/api/monitor/logs":
            self.monitor_logs()
        elif self.path == "/api/friends":
            self.list_friends()
        elif urlsplit(self.path).path.startswith("/api/images/"):
            self.send_note_image()
        elif urlsplit(self.path).path.startswith("/api/users/") and urlsplit(self.path).path.endswith("/avatar"):
            self.send_avatar()
        elif urlsplit(self.path).path.startswith("/api/notes/") and urlsplit(self.path).path.endswith("/members"):
            self.list_note_members()
        elif self.path == "/api/notes":
            self.list_notes()
        # ─── 金融账本 ───
        elif self.path == "/api/ledgers":
            self.list_ledgers()
        elif urlsplit(self.path).path.endswith("/transactions") and urlsplit(self.path).path.startswith("/api/ledgers/"):
            self.list_ledger_transactions()
        elif urlsplit(self.path).path.endswith("/members") and urlsplit(self.path).path.startswith("/api/ledgers/"):
            self.list_ledger_members()
        elif urlsplit(self.path).path.endswith("/history") and urlsplit(self.path).path.startswith("/api/ledgers/"):
            self.list_ledger_history()
        elif urlsplit(self.path).path.endswith("/settlement") and urlsplit(self.path).path.startswith("/api/ledgers/"):
            self.ledger_settlement()
        else:
            self.send_json(404, {"error": "Not found"})

    def stream_events(self):
        """Server-Sent Events：把筆記與在線狀態的變更即時推給所有前端。"""
        parsed = urlsplit(self.path)
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            # EventSource 無法自訂標頭，因此允許以 query string 傳遞 token
            token = (parse_qs(parsed.query).get("token") or [""])[0].strip()
        session = get_session(token)
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
        log_event(f"{session['username']} 開啟即時事件連線", "EVENT")
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
            log_event(f"{session['username']} 關閉即時事件連線", "EVENT")

    def monitor_allowed(self):
        """監控端點預設只開放本機；設定 COLLABNOTE_MONITOR_TOKEN 後可用標頭存取。"""
        client = self.client_address[0] if self.client_address else ""
        if client in ("127.0.0.1", "::1"):
            return True
        if not MONITOR_TOKEN:
            return False
        supplied = self.headers.get("X-Monitor-Token", "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, MONITOR_TOKEN)

    def monitor_status(self):
        if not self.monitor_allowed():
            self.send_json(403, {"error": "監控端點只允許本機存取"})
            return
        self.send_json(200, monitor_snapshot())

    def monitor_logs(self):
        """Server-Sent Events：先把緩衝內的舊日誌補上，之後即時推送新日誌。"""
        if not self.monitor_allowed():
            self.send_json(403, {"error": "監控端點只允許本機存取"})
            return
        subscriber_queue = Queue(maxsize=LOG_SUBSCRIBER_QUEUE_SIZE)
        client_id = secrets.token_hex(8)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        with LOG_LOCK:
            backlog = list(LOG_BUFFER)
            LOG_SUBSCRIBERS[client_id] = subscriber_queue
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.write(b": connected\n\n")
            self.wfile.write(("data: " + json.dumps({"type": "backlog", "lines": backlog}, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    entry = subscriber_queue.get(timeout=EVENT_HEARTBEAT)
                except Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(entry if entry.get("type") else {"type": "line", **entry}, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with LOG_LOCK:
                LOG_SUBSCRIBERS.pop(client_id, None)

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
        resource = urlsplit(self.path).path
        if resource.startswith("/api/notes/") and resource.endswith("/members"):
            self.add_note_member()
            return
        # ─── 金融账本 ───
        if resource == "/api/ledgers":
            self.create_ledger()
            return
        if resource.startswith("/api/ledgers/") and resource.endswith("/transactions"):
            self.add_ledger_transaction()
            return
        if resource.startswith("/api/ledgers/") and resource.endswith("/members"):
            self.add_ledger_member()
            return
        if resource.startswith("/api/ledgers/") and resource.endswith("/restore"):
            self.restore_ledger_history()
            return
        if self.path != "/api/notes":
            self.send_json(404, {"error": "Not found"})
            return
        self.create_note()

    def do_PUT(self):
        resource = urlsplit(self.path).path
        # ─── 金融账本：編輯交易 / 編輯賬本（預算等）───
        if resource.startswith("/api/ledgers/") and "/transactions/" in resource:
            self.update_ledger_transaction()
            return
        if resource.startswith("/api/ledgers/") and resource[len("/api/ledgers/"):].isdigit():
            self.update_ledger_info()
            return
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
        session = self.current_user()
        with DATA_LOCK:
            notes = load_notes()
            for note in notes:
                if note["id"] != note_id:
                    continue
                if not note_can_edit(note, session["username"]):
                    self.send_json(403, {"error": "只有編輯權限才能修改這篇筆記"})
                    return
                if "title" in payload:
                    note["title"] = str(payload["title"])[:255]
                if "content" in payload:
                    note["content"] = normalize_note_content(payload["content"])
                save_notes(notes)
                result = public_note(note, session["username"])
                publish_note_event("note-updated", note, self.current_session_sid())
                log_event(f"{session['username']} 更新筆記 #{note['id']}「{note.get('title', '')}」")
                self.send_json(200, result)
                return
        self.send_json(404, {"error": "Note not found"})

    def do_DELETE(self):
        resource = urlsplit(self.path).path
        # ─── 金融账本：软删除交易 ───
        if resource.startswith("/api/ledgers/") and "/transactions/" in resource:
            self.delete_ledger_transaction()
            return
        if resource.startswith("/api/ledgers/") and "/members/" in resource:
            self.remove_ledger_member()
            return
        if resource.startswith("/api/ledgers/") and resource[len("/api/ledgers/"):].isdigit():
            self.delete_ledger()
            return
        if resource.startswith("/api/notes/") and "/members/" in resource:
            self.remove_note_member()
            return
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

        session = self.current_user()
        with DATA_LOCK:
            notes = load_notes()
            note = next((item for item in notes if item["id"] == note_id), None)
            if note is None:
                self.send_json(404, {"error": "Note not found"})
                return
            if note_access(note, session["username"]) != "owner":
                self.send_json(403, {"error": "只有建立者可以刪除筆記"})
                return
            remaining_notes = [item for item in notes if item["id"] != note_id]
            save_notes(remaining_notes)
        publish_note_change("note-deleted", note, self.current_session_sid())
        log_event(f"{session['username']} 刪除筆記 #{note_id}「{note.get('title', '')}」", "WARN")
        self.send_json(200, {"deleted": note_id})

    def list_notes(self):
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return
        username = session["username"]
        with DATA_LOCK:
            notes = load_notes()
        # 沒有權限的筆記完全不回傳
        visible = [public_note(note, username) for note in notes if note_visible_to(note, username)]
        self.send_json(200, visible)

    def create_note(self):
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return
        payload = self.read_payload()
        with DATA_LOCK:
            notes = load_notes()
            note = {
                "title": str(payload.get("title", "未命名筆記"))[:255],
                "tag": "草稿",
                "tagStyle": "background:#f1f5f9;color:#475569;",
                "content": normalize_note_content(payload.get("content", "開始撰寫你的筆記…")),
                "time": "剛剛",
                "owner": session["username"],
                "members": [],
            }
            note = storage().create_note(note)
        result = public_note(note, session["username"])
        publish_note_event("note-created", note, self.current_session_sid())
        log_event(f"{session['username']} 新增筆記 #{note['id']}「{note['title']}」")
        self.send_json(201, result)

    # ═══════════ 金融账本 API ═══════════
    def ledger_guard(self, ledger_id, action):
        """校验当前用户对账本的权限，返回 (session, role)；不通过时已响应。"""
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return None, None
        role = ledger_store().role_of(ledger_id, session["username"])
        if not ledger_can(role, action):
            self.send_json(403, {"error": "沒有這項權限"})
            return None, None
        return session, role

    def list_ledgers(self):
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return
        self.send_json(200, ledger_store().list_ledgers(session["username"]))

    def create_ledger(self):
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return
        payload = self.read_payload()
        name = str(payload.get("name", "")).strip() or "未命名账本"
        icon = str(payload.get("icon", "fa-book")).strip() or "fa-book"
        # 只允许类名安全字符，避免写入 HTML 时 XSS
        icon = re.sub(r"[^A-Za-z0-9_-]", "", icon)[:64] or "fa-book"
        try:
            budget = float(payload.get("budget", 0) or 0)
        except (TypeError, ValueError):
            budget = 0
        ledger_id = ledger_store().create_ledger(session["username"], name[:128], icon[:64], max(0, budget))
        log_event(f"{session['username']} 建立賬本 #{ledger_id}「{name}」")
        self.send_json(201, {"id": ledger_id, "name": name})

    def list_ledger_transactions(self):
        ledger_id = self._ledger_id_from_path("/transactions")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "view")
        if session is None:
            return
        include_deleted = role == "owner" and (parse_qs(urlsplit(self.path).query).get("deleted") or [""])[0] == "1"
        self.send_json(200, ledger_store().list_transactions(ledger_id, include_deleted=include_deleted))

    def list_ledger_members(self):
        ledger_id = self._ledger_id_from_path("/members")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "view")
        if session is None:
            return
        self.send_json(200, {
            "members": ledger_store().list_members(ledger_id),
            "myRole": role,
        })

    def list_ledger_history(self):
        ledger_id = self._ledger_id_from_path("/history")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "view")
        if session is None:
            return
        self.send_json(200, ledger_store().list_history(ledger_id))

    def add_ledger_transaction(self):
        ledger_id = self._ledger_id_from_path("/transactions")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "write")
        if session is None:
            return
        payload = self.read_payload()
        txn_type = "income" if payload.get("type") == "income" else "expense"
        try:
            amount = abs(float(payload.get("amount", 0) or 0))
        except (TypeError, ValueError):
            self.send_json(400, {"error": "金額格式不正確"})
            return
        if amount <= 0:
            self.send_json(400, {"error": "金額必須大於 0"})
            return
        category = str(payload.get("category", "其他")).strip()[:32] or "其他"
        note = str(payload.get("note", "")).strip()[:255]
        paid_by = str(payload.get("paidBy", "")).strip()[:32] or session["username"]
        split_members = payload.get("splitMembers")
        if not isinstance(split_members, list):
            split_members = None
        else:
            valid = {m["username"] for m in ledger_store().list_members(ledger_id)}
            split_members = [str(x) for x in split_members if str(x) in valid]
            if not split_members:
                split_members = None
        txn_id = ledger_store().add_transaction(
            ledger_id, session["username"], txn_type, amount, category, note,
            paid_by=paid_by, split_members=split_members,
        )
        log_event(f"{session['username']} 在賬本 #{ledger_id} 記一筆（{category} {amount}）")
        publish_ledger_event(ledger_id, self.current_session_sid())
        self.send_json(201, {"id": txn_id})

    def update_ledger_info(self):
        resource = urlsplit(self.path).path
        try:
            ledger_id = int(resource[len("/api/ledgers/"):])
        except ValueError:
            self.send_json(400, {"error": "Invalid ledger id"})
            return
        # 編輯賬本資訊：擁有者/管理皆可
        session, role = self.ledger_guard(ledger_id, "manage")
        if session is None:
            return
        payload = self.read_payload()
        ok = ledger_store().update_ledger(
            ledger_id,
            name=payload.get("name"),
            icon=payload.get("icon"),
            budget=payload.get("budget"),
        )
        if not ok:
            self.send_json(400, {"error": "沒有要修改的內容"})
            return
        log_event(f"{session['username']} 更新賬本 #{ledger_id}")
        publish_ledger_event(ledger_id, self.current_session_sid())
        self.send_json(200, {"updated": ledger_id})

    def delete_ledger(self):
        resource = urlsplit(self.path).path
        try:
            ledger_id = int(resource[len("/api/ledgers/"):])
        except ValueError:
            self.send_json(400, {"error": "Invalid ledger id"})
            return
        session, role = self.ledger_guard(ledger_id, "manage")
        if session is None:
            return
        if not ledger_store().soft_delete_ledger(ledger_id, session["username"]):
            self.send_json(404, {"error": "账本不存在"})
            return
        log_event(f"{session['username']} 刪除賬本 #{ledger_id}（可復原）", "WARN")
        publish_ledger_event(ledger_id, self.current_session_sid())
        self.send_json(200, {"deleted": ledger_id})

    def update_ledger_transaction(self):
        ledger_id = self._ledger_id_from_path("/transactions/")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "edit")
        if session is None:
            return
        parts = urlsplit(self.path).path.split("/transactions/")
        try:
            txn_id = int(parts[1])
        except (IndexError, ValueError):
            self.send_json(400, {"error": "Invalid transaction id"})
            return
        payload = self.read_payload()
        changes = {}
        for key in ("note", "category", "amount", "type", "split_members"):
            if key in payload:
                changes[key] = payload[key]
        if "splitMembers" in payload:
            valid = {m["username"] for m in ledger_store().list_members(ledger_id)}
            raw = payload.get("splitMembers")
            changes["split_members"] = [str(x) for x in raw if str(x) in valid] if isinstance(raw, list) else None
        if not changes:
            self.send_json(400, {"error": "沒有要修改的內容"})
            return
        ok = ledger_store().update_transaction(ledger_id, txn_id, session["username"], changes)
        if not ok:
            self.send_json(400, {"error": "資料不合法或交易不存在"})
            return
        log_event(f"{session['username']} 修改賬本 #{ledger_id} 的交易 #{txn_id}")
        publish_ledger_event(ledger_id, self.current_session_sid())
        self.send_json(200, {"updated": txn_id})

    def delete_ledger_transaction(self):
        ledger_id = self._ledger_id_from_path("/transactions/")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "delete")
        if session is None:
            return
        parts = urlsplit(self.path).path.split("/transactions/")
        try:
            txn_id = int(parts[1])
        except (IndexError, ValueError):
            self.send_json(400, {"error": "Invalid transaction id"})
            return
        snapshot = ledger_store().soft_delete_transaction(ledger_id, txn_id, session["username"])
        if snapshot is None:
            self.send_json(404, {"error": "交易不存在"})
            return
        log_event(f"{session['username']} 刪除賬本 #{ledger_id} 的交易 #{txn_id}（已備份可復原）", "WARN")
        publish_ledger_event(ledger_id, self.current_session_sid())
        self.send_json(200, {"deleted": txn_id})

    def add_ledger_member(self):
        ledger_id = self._ledger_id_from_path("/members")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "invite")
        if session is None:
            return
        payload = self.read_payload()
        username = str(payload.get("username", "")).strip()
        new_role = str(payload.get("role", "viewer"))
        if new_role not in LEDGER_ROLE_RANK or new_role == "owner":
            self.send_json(400, {"error": "權限不合法"})
            return
        if username == session["username"]:
            self.send_json(400, {"error": "自己已經在名單中"})
            return
        if not find_user(username):
            self.send_json(404, {"error": "找不到該使用者"})
            return
        # admin 不能授權 admin
        if new_role == "admin" and role != "owner":
            self.send_json(403, {"error": "只有擁有者能授予管理權限"})
            return
        ledger_store().set_member_role(ledger_id, username, new_role)
        log_event(f"{session['username']} 邀請 {username} 加入賬本 #{ledger_id}（{new_role}）")
        publish_ledger_event(ledger_id, self.current_session_sid(), member_usernames=[username])
        self.send_json(200, {"members": ledger_store().list_members(ledger_id)})

    def remove_ledger_member(self):
        ledger_id = self._ledger_id_from_path("/members/")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "invite")
        if session is None:
            return
        username = unquote(urlsplit(self.path).path.split("/members/", 1)[1])
        if not ledger_store().remove_member(ledger_id, username):
            self.send_json(404, {"error": "對方不在名單中或為擁有者"})
            return
        log_event(f"{session['username']} 移除賬本 #{ledger_id} 成員 {username}", "WARN")
        publish_ledger_event(ledger_id, self.current_session_sid(), member_usernames=[username])
        self.send_json(200, {"members": ledger_store().list_members(ledger_id)})

    def restore_ledger_history(self):
        ledger_id = self._ledger_id_from_path("/restore")
        if ledger_id is None:
            return
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return
        # 只有擁有者能復原
        if ledger_store().role_of(ledger_id, session["username"]) != "owner":
            self.send_json(403, {"error": "只有擁有者可以復原"})
            return
        payload = self.read_payload()
        try:
            history_id = int(payload.get("historyId"))
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Invalid history id"})
            return
        ok = ledger_store().restore_transaction(ledger_id, history_id, session["username"])
        if not ok:
            self.send_json(404, {"error": "記錄不存在或無法復原"})
            return
        log_event(f"{session['username']} 復原賬本 #{ledger_id} 的記錄 #{history_id}", "WARN")
        publish_ledger_event(ledger_id, self.current_session_sid())
        self.send_json(200, {"restored": history_id})

    def ledger_settlement(self):
        ledger_id = self._ledger_id_from_path("/settlement")
        if ledger_id is None:
            return
        session, role = self.ledger_guard(ledger_id, "view")
        if session is None:
            return
        query = parse_qs(urlsplit(self.path).query)
        start = (query.get("start") or [""])[0].strip() or None
        end = (query.get("end") or [""])[0].strip() or None
        transfers = ledger_store().settlement(ledger_id, start=start, end=end)
        self.send_json(200, {"transfers": transfers, "start": start, "end": end})

    def _ledger_id_from_path(self, suffix):
        resource = urlsplit(self.path).path
        if not resource.startswith("/api/ledgers/"):
            self.send_json(404, {"error": "Not found"})
            return None
        tail = resource[len("/api/ledgers/"):]
        cut = tail.find("/")
        if cut <= 0:
            self.send_json(400, {"error": "Invalid ledger id"})
            return None
        try:
            return int(tail[:cut])
        except ValueError:
            self.send_json(400, {"error": "Invalid ledger id"})
            return None

    def note_members_payload(self, note, username):
        owner_record = find_user(note.get("owner") or "")

        def people(names, access):
            result = []
            for name in names:
                record = find_user(name)
                if record:
                    result.append({**public_user(record), "access": access})
            return result

        return {
            "noteId": note["id"],
            "owner": public_user(owner_record) if owner_record else None,
            "members": people(note.get("members") or [], "edit"),
            "viewers": people(note.get("viewers") or [], "view"),
            "canManage": note_access(note, username) == "owner",
        }

    def read_note_for_member_action(self):
        """解析 /api/notes/<id>/members... 並確認呼叫者可以檢視這篇筆記。"""
        resource = urlsplit(self.path).path
        parts = resource.split("/")
        try:
            note_id = int(parts[3])
        except (IndexError, ValueError):
            self.send_json(400, {"error": "Invalid note id"})
            return None, None
        session = self.current_user()
        if not session:
            self.send_json(401, {"error": "Authentication required"})
            return None, None
        with DATA_LOCK:
            note = next((item for item in load_notes() if item["id"] == note_id), None)
        if note is None:
            self.send_json(404, {"error": "Note not found"})
            return None, None
        if not note_visible_to(note, session["username"]):
            self.send_json(403, {"error": "沒有這篇筆記的權限"})
            return None, None
        return note, session

    def list_note_members(self):
        note, session = self.read_note_for_member_action()
        if note is None:
            return
        self.send_json(200, self.note_members_payload(note, session["username"]))

    def add_note_member(self):
        note, session = self.read_note_for_member_action()
        if note is None:
            return
        if note_access(note, session["username"]) != "owner":
            self.send_json(403, {"error": "只有建立者可以邀請協作"})
            return
        payload_input = self.read_payload()
        username = str(payload_input.get("username", "")).strip()
        access = "view" if payload_input.get("access") == "view" else "edit"
        if not USERNAME_PATTERN.fullmatch(username):
            self.send_json(400, {"error": "请输入有效的登录名"})
            return
        if username == session["username"]:
            self.send_json(400, {"error": "建立者已經擁有權限"})
            return
        if not find_user(username):
            self.send_json(404, {"error": "找不到该用户"})
            return
        with DATA_LOCK:
            notes = load_notes()
            for item in notes:
                if item["id"] != note["id"]:
                    continue
                members = item.setdefault("members", [])
                viewers = item.setdefault("viewers", [])
                if (username in members and access == "edit") or (username in viewers and access == "view"):
                    self.send_json(409, {"error": "對方已經是這個權限"})
                    return
                # 已在另一份名單時視為調整權限
                item["members"] = [name for name in members if name != username]
                item["viewers"] = [name for name in viewers if name != username]
                (item["members"] if access == "edit" else item["viewers"]).append(username)
                save_notes(notes)
                updated = dict(item)
                break
            else:
                self.send_json(404, {"error": "Note not found"})
                return
        payload = self.note_members_payload(updated, session["username"])
        publish_note_change("note-members", updated, self.current_session_sid())
        log_event(f"{session['username']} 邀請 {username} 協作 #{updated['id']}（{'可編輯' if access == 'edit' else '只讀'}）")
        self.send_json(200, payload)

    def remove_note_member(self):
        note, session = self.read_note_for_member_action()
        if note is None:
            return
        if note_access(note, session["username"]) != "owner":
            self.send_json(403, {"error": "只有建立者可以移除協作"})
            return
        username = unquote(urlsplit(self.path).path.split("/members/", 1)[1])
        with DATA_LOCK:
            notes = load_notes()
            for item in notes:
                if item["id"] != note["id"]:
                    continue
                members = item.get("members") or []
                viewers = item.get("viewers") or []
                if username not in members and username not in viewers:
                    self.send_json(404, {"error": "對方不在協作名單中"})
                    return
                item["members"] = [name for name in members if name != username]
                item["viewers"] = [name for name in viewers if name != username]
                save_notes(notes)
                updated = dict(item)
                break
            else:
                self.send_json(404, {"error": "Note not found"})
                return
        payload = self.note_members_payload(updated, session["username"])
        publish_note_change("note-members", updated, self.current_session_sid())
        log_event(f"{session['username']} 移除協作者 {username} #{updated['id']}", "WARN")
        # 被移除的人不會再收到這篇筆記的事件，因此單獨通知他把筆記從清單移除
        publish_to_users({"type": "note-unshared", "noteId": updated["id"], "sid": self.current_session_sid()}, [username])
        self.send_json(200, payload)

    MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

    def read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length > self.MAX_PAYLOAD_BYTES:
            self.send_json(413, {"error": "請求內容過大"})
            self.close_connection = True
            raise BadPayload()
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
        with DATA_LOCK:
            note = next((item for item in load_notes() if item["id"] == note_id), None)
        if note is not None and not note_visible_to(note, session["username"]):
            self.send_json(403, {"error": "沒有這篇筆記的權限"})
            return
        with PRESENCE_LOCK:
            previous = PRESENCE.get(token)
            PRESENCE[token] = {
                "username": session["username"],
                "noteId": note_id,
                "mode": mode,
                "ts": time.monotonic(),
            }
        if previous is None or previous.get("noteId") != note_id or previous.get("mode") != mode:
            title = note.get("title", "") if note else ""
            action = "編輯中" if mode == "editing" else "閱讀中"
            log_event(f"{session['username']} {action} #{note_id}「{title}」", "PRESENCE")
        people = presence_people_for(note_id, session["username"])
        publish_presence(note_id)
        self.send_json(200, {"noteId": note_id, "people": people})

    def current_session_sid(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        session = get_session(token)
        return session.get("sid", "") if session else ""

    def current_user(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        return get_session(token)

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
        log_event(f"{user['username']} {'接受' if action == 'accept' else '拒絕'} {requester_name} 的好友邀請")
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
        log_event(f"新使用者註冊：{username}")
        self.send_json(201, {"token": token, **session_public(SESSIONS[token]), **public_user(user)})

    def login_user(self):
        payload = self.read_payload()
        username = str(payload.get("username", "")).strip()
        client_ip = self.client_address[0] if self.client_address else "?"
        guard_key = f"{client_ip}|{username}"
        allowed, retry = login_attempts_guard(guard_key)
        if not allowed:
            log_event(f"登入受限（嘗試過多）：{username} @ {client_ip}", "WARN")
            self.send_json(429, {"error": f"嘗試次數過多，請 {retry} 秒後再試"})
            return
        user = authenticate_user(username, str(payload.get("password", "")))
        if not user:
            record_login_failure(guard_key)
            log_event(f"登入失敗：{username or '(空)'}", "WARN")
            self.send_json(401, {"error": "用户名或密码错误"})
            return
        clear_login_failures(guard_key)
        token = create_session(user)
        log_event(f"{user['username']} 登入成功")
        self.send_json(200, {"token": token, **session_public(SESSIONS[token]), **public_user(user)})

    def logout_user(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ").strip()
        session = SESSIONS.pop(token, None)
        if session:
            log_event(f"{session['username']} 登出")
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
        # 昵称写回前端会被插入 HTML，去掉尖括号与引号
        display_name = re.sub(r"[<>\"'`]", "", display_name)
        title = str(payload.get("title", user.get("title", "团队成员"))).strip()
        avatar_data = payload.get("avatarData")
        if not 1 <= len(display_name) <= 32:
            self.send_json(400, {"error": "昵称长度需为 1-32 个字符"})
            return
        if len(title) > 40:
            self.send_json(400, {"error": "头衔不能超过 40 个字符"})
            return

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
                    log_event(f"{user['username']} 更新個人資料（暱稱：{display_name}）")
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
        session = self.current_user()
        log_event(f"{session['username'] if session else '-'} 上傳圖片 {filename}（{length // 1024} KB）")
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

    def log_request(self, code="-", size="-"):
        """把每個請求寫進監控日誌；靜態資源（頭像、圖片、健康檢查）另外標記。"""
        resource = urlsplit(getattr(self, "path", "")).path
        if resource in ("/api/health", "/api/monitor/logs", "/api/monitor/status") or resource.startswith("/api/users/") or resource.startswith("/api/images/"):
            level = "STATIC"
        else:
            level = "HTTP"
        session = self.current_user()
        who = session["username"] if session else "-"
        log_event(f"{getattr(self, 'command', '-')} {resource} -> {code} ({who})", level)


def applescript_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def monitor_command():
    """監控器指令；固定走 127.0.0.1，因為監控端點只開放本機。"""
    host = HOST if HOST not in ("0.0.0.0", "::", "") else "127.0.0.1"
    if getattr(sys, "frozen", False):
        # 打包後沒有 monitor.py，改用同一支執行檔的 --monitor 模式
        parts = [shlex.quote(sys.executable), "--monitor"]
    else:
        parts = [shlex.quote(sys.executable), shlex.quote(str(BASE_DIR / "monitor.py"))]
    parts += [
        "--url", shlex.quote("http://%s:%d" % (host, PORT)),
        "--title", shlex.quote(MONITOR_WINDOW_TITLE),
        "--pid-file", shlex.quote(str(monitor_pid_file())),
        "--exit-when-offline", "4",
    ]
    return " ".join(parts)


def monitor_pid_file():
    return Path(gettempdir()) / ("collabnote-monitor-%d.pid" % PORT)


def open_monitor_window():
    """在新終端視窗啟動監控器，後端結束時會一併收掉。"""
    if not MONITOR_WINDOW:
        return
    if not getattr(sys, "frozen", False) and not (BASE_DIR / "monitor.py").exists():
        return
    try:
        monitor_pid_file().unlink()
    except OSError:
        pass
    command = monitor_command()
    try:
        if sys.platform == "darwin":
            # 不把含中文与多层 shell 引号的整串命令直接塞进 AppleScript，
            # 先写一个临时 command 文件，再让 Terminal 执行它，避免 -2741 解析错误。
            launch_file = Path(gettempdir()) / ("collabnote-monitor-launch-%d.command" % PORT)
            launch_file.write_text(
                "#!/bin/bash\n"
                "rm -f %s\n"
                "exec %s\n" % (shlex.quote(str(launch_file)), command),
                encoding="utf-8",
            )
            launch_file.chmod(0o700)
            script = 'tell application "Terminal"\n  do script %s\n  activate\nend tell' % applescript_quote("bash " + shlex.quote(str(launch_file)))
            result = subprocess.run(["osascript", "-e", script],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                log_event("無法開啟監控視窗：osascript " + (result.stderr.strip() or "exit %d" % result.returncode), "WARN")
        elif sys.platform.startswith("win"):
            subprocess.Popen('start "%s" cmd /c "%s"' % (MONITOR_WINDOW_TITLE, command),
                             shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            for terminal, flags in (("x-terminal-emulator", ["-e", "bash", "-lc"]),
                                    ("gnome-terminal", ["--", "bash", "-lc"]),
                                    ("konsole", ["-e", "bash", "-lc"]),
                                    ("xterm", ["-e", "bash", "-lc"])):
                if shutil.which(terminal):
                    subprocess.Popen([terminal] + flags + [command],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
    except OSError as error:
        log_event(f"無法開啟監控視窗：{error}", "WARN")


def close_monitor_window():
    """等監控器真的結束後才關掉它的終端視窗（延後執行，不拖慢後端關閉）。"""
    if not MONITOR_WINDOW or sys.platform != "darwin":
        return
    script = ('tell application "Terminal"\n'
              '  repeat with target in (every window whose name contains %s)\n'
              '    close target\n'
              '  end repeat\n'
              'end tell') % applescript_quote(MONITOR_WINDOW_TITLE)
    pid_file = shlex.quote(str(monitor_pid_file()))
    # 先等監控器（pid 檔）消失，避免在還有行程執行時關窗而跳出確認對話框
    helper = (
        "for _ in $(seq 1 120); do "
        "[ -f %s ] || break; "
        "pid=$(cat %s 2>/dev/null); "
        "[ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null || break; "
        "sleep 0.5; "
        "done; sleep 0.5; osascript -e %s"
    ) % (pid_file, pid_file, shlex.quote(script))
    try:
        subprocess.Popen(["bash", "-c", helper], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


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
    # 打包後的執行檔同時扮演監控器：`--monitor` 等同執行 monitor.py
    if "--monitor" in sys.argv[1:]:
        import monitor
        sys.exit(monitor.main([arg for arg in sys.argv[1:] if arg != "--monitor"]))
    # 舊筆記沒有建立者欄位，開機時補上預設的擁有者與協作名單
    migrate_note_permissions()
    # 舊版把頭像與筆記圖片存在前端目錄，開機時搬到後端的 data/
    migrate_legacy_uploads()
    server = NotesServer((HOST, PORT), NotesHandler)
    print_banner()
    log_event(f"伺服器啟動：http://{HOST}:{PORT}")

    def stop_on_signal(signum, frame):
        # launcher 用 SIGTERM 關閉後端；直接退出例外會讓凍結版印出 traceback。
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_signal)
    # 開一個終端視窗跑監控器（COLLABNOTE_MONITOR_WINDOW=0 可關閉）
    open_monitor_window()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log_event("伺服器已停止", "WARN")
        notify_monitors_shutdown()
        # 留一點時間讓 SSE 把結束通知送出去
        time.sleep(0.3)
        close_monitor_window()
