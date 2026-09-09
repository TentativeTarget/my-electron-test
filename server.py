from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from threading import Lock

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
DATA_FILE = Path(__file__).with_name("notes.json")
DATA_LOCK = Lock()


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/notes":
            with DATA_LOCK:
                self.send_json(200, load_notes())
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/notes":
            self.send_json(404, {"error": "Not found"})
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
