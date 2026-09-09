from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
DATA_FILE = Path(__file__).with_name("notes.json")

DEFAULT_NOTES = [
    {
        "id": 1,
        "title": "產品願景與策略",
        "tag": "重要",
        "tagStyle": "background:#dbeafe;color:#2563eb;",
        "content": "## 產品願景\n\n建立一個讓團隊無縫協作的筆記平台，將**靈感**與**執行**連結在一起。\n\n### 核心目標\n- 即時同步，零延遲\n- 直覺的編輯體驗\n- 強大的搜尋與標籤",
        "time": "10 分鐘前",
        "editors": ["green", "purple"],
    },
    {
        "id": 2,
        "title": "Q4 行銷計劃",
        "tag": "進行中",
        "tagStyle": "background:#fef3c7;color:#b45309;",
        "content": "## Q4 行銷計劃\n\n目標：提升品牌知名度與用戶轉化。",
        "time": "1 小時前",
        "editors": ["yellow", "blue", "pink"],
    },
    {
        "id": 3,
        "title": "設計系統 2.0",
        "tag": "已完成",
        "tagStyle": "background:#d1fae5;color:#065f46;",
        "content": "## 設計系統 2.0\n\n已完成設計系統的全面升級。",
        "time": "昨天",
        "editors": ["green"],
    },
    {
        "id": 4,
        "title": "使用者訪談彙整",
        "tag": "回饋",
        "tagStyle": "background:#e0e7ff;color:#3730a3;",
        "content": "## 使用者訪談彙整\n\n共訪談 12 位使用者，歸納出以下痛點。",
        "time": "2 天前",
        "editors": ["purple", "blue"],
    },
    {
        "id": 5,
        "title": "工程架構決策",
        "tag": "待定",
        "tagStyle": "background:#fee2e2;color:#991b1b;",
        "content": "## 工程架構決策\n\n討論新的微服務架構與資料庫選擇。",
        "time": "3 天前",
        "editors": ["green", "yellow", "pink"],
    },
]


def load_notes():
    if not DATA_FILE.exists():
        save_notes(DEFAULT_NOTES)
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return list(DEFAULT_NOTES)


def save_notes(notes):
    DATA_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/health":
            self.send_json(200, {"ok": True})
        elif self.path == "/api/notes":
            self.send_json(200, load_notes())
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/notes":
            self.send_json(404, {"error": "Not found"})
            return
        payload = self.read_payload()
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
        try:
            note_id = int(self.path.rsplit("/", 1)[1])
        except ValueError:
            self.send_json(400, {"error": "Invalid note id"})
            return
        payload = self.read_payload()
        notes = load_notes()
        for note in notes:
            if note["id"] == note_id:
                note.update({key: payload[key] for key in ("title", "content") if key in payload})
                save_notes(notes)
                self.send_json(200, note)
                return
        self.send_json(404, {"error": "Note not found"})

    def read_payload(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), NotesHandler)
    print(f"Notes API listening on http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
