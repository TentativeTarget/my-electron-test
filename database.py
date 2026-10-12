"""CollabNote storage backends.

MySQL is the production backend. JSON remains available only as an explicit
compatibility/fallback backend and for importing existing installations.
"""
from contextlib import contextmanager
import json
import os
import sys
from pathlib import Path


class StorageConfigError(RuntimeError):
    pass


class JsonStorage:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.notes_file = self.base_dir / "notes.json"
        self.users_file = self.base_dir / "users.json"

    @staticmethod
    def _read(path):
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _write(path, records):
        from tempfile import NamedTemporaryFile
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, path)

    def load_notes(self):
        return self._read(self.notes_file)

    def save_notes(self, notes):
        self._write(self.notes_file, notes)

    def create_note(self, note):
        # JSON compatibility mode still needs a collision-free numeric id.
        notes = self.load_notes()
        highest = max((int(item.get("id", 0)) for item in notes), default=0)
        note = {**note, "id": highest + 1}
        notes.insert(0, note)
        self.save_notes(notes)
        return note

    def load_users(self):
        return self._read(self.users_file)

    def save_users(self, users):
        self._write(self.users_file, users)

    def close(self):
        pass


class MySQLStorage:
    def __init__(self, config=None):
        self.config = config or mysql_config_from_env()
        try:
            import pymysql
        except ImportError as error:
            raise StorageConfigError(
                "MySQL storage requires PyMySQL. Run: python3 -m pip install -r requirements.txt"
            ) from error
        self.pymysql = pymysql

    @contextmanager
    def connection(self):
        connection = self.pymysql.connect(
            host=self.config["host"],
            port=self.config["port"],
            user=self.config["user"],
            password=self.config["password"],
            database=self.config["database"],
            charset="utf8mb4",
            autocommit=False,
            cursorclass=self.pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self, schema_path=None):
        if schema_path:
            path = Path(schema_path)
        elif getattr(sys, "frozen", False):
            path = Path(sys.executable).resolve().parent / "schema.sql"
        else:
            path = Path(__file__).with_name("schema.sql")
        sql = path.read_text(encoding="utf-8")
        statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
        with self.connection() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
            connection.commit()

    def load_users(self):
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, username, password_salt, password_hash,
                           display_name, title, avatar_file
                    FROM users ORDER BY id
                """)
                rows = cursor.fetchall()
                cursor.execute("SELECT user_id, friend_id FROM friendships")
                friendships = cursor.fetchall()
                cursor.execute("""
                    SELECT requester_id, target_id FROM friend_requests
                    WHERE status = 'pending'
                """)
                requests = cursor.fetchall()
        names = {row["id"]: row["username"] for row in rows}
        friends = {row["id"]: [] for row in rows}
        incoming = {row["id"]: [] for row in rows}
        for relation in friendships:
            left, right = relation["user_id"], relation["friend_id"]
            if left in names and right in names:
                friends[left].append(names[right])
                friends[right].append(names[left])
        for request in requests:
            if request["requester_id"] in names and request["target_id"] in names:
                incoming[request["target_id"]].append(names[request["requester_id"]])
        result = []
        for row in rows:
            user = {
                "username": row["username"],
                "salt": row["password_salt"],
                "passwordHash": row["password_hash"],
                "friends": sorted(set(friends[row["id"]])),
                "friendRequests": sorted(set(incoming[row["id"]])),
            }
            for source, target in (("display_name", "displayName"), ("title", "title"), ("avatar_file", "avatarFile")):
                if row[source] is not None:
                    user[target] = row[source]
            result.append(user)
        return result

    def save_users(self, users):
        usernames = {str(user.get("username", "")) for user in users if user.get("username")}
        with self.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    for user in users:
                        cursor.execute("""
                            INSERT INTO users
                                (username, password_salt, password_hash, display_name, title, avatar_file)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                password_salt=VALUES(password_salt),
                                password_hash=VALUES(password_hash),
                                display_name=VALUES(display_name),
                                title=VALUES(title),
                                avatar_file=VALUES(avatar_file)
                        """, (
                            user["username"], user["salt"], user["passwordHash"],
                            user.get("displayName"), user.get("title"), user.get("avatarFile"),
                        ))
                    if usernames:
                        placeholders = ",".join(["%s"] * len(usernames))
                        cursor.execute(f"DELETE FROM users WHERE username NOT IN ({placeholders})", tuple(usernames))
                    else:
                        cursor.execute("DELETE FROM users")
                    cursor.execute("SELECT id, username FROM users")
                    ids = {row["username"]: row["id"] for row in cursor.fetchall()}
                    cursor.execute("DELETE FROM friendships")
                    cursor.execute("DELETE FROM friend_requests WHERE status = 'pending'")
                    friend_pairs = set()
                    pending_pairs = set()
                    for user in users:
                        source_id = ids[user["username"]]
                        for friend in user.get("friends", []):
                            if friend in ids and friend != user["username"]:
                                friend_pairs.add(tuple(sorted((source_id, ids[friend]))))
                        for requester in user.get("friendRequests", []):
                            if requester in ids and requester != user["username"]:
                                pending_pairs.add((ids[requester], source_id))
                    if friend_pairs:
                        cursor.executemany(
                            "INSERT INTO friendships (user_id, friend_id) VALUES (%s, %s)",
                            sorted(friend_pairs),
                        )
                    if pending_pairs:
                        cursor.executemany("""
                            INSERT INTO friend_requests (requester_id, target_id, status)
                            VALUES (%s, %s, 'pending')
                        """, sorted(pending_pairs))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def load_notes(self):
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT n.id, n.title, n.content, n.tag, n.tag_style, n.time_label,
                           owner.username AS owner
                    FROM notes n JOIN users owner ON owner.id = n.owner_id
                    ORDER BY n.id DESC
                """)
                rows = cursor.fetchall()
                cursor.execute("""
                    SELECT nm.note_id, u.username, nm.permission
                    FROM note_members nm JOIN users u ON u.id = nm.user_id
                """)
                member_rows = cursor.fetchall()
        members = {}
        viewers = {}
        for row in member_rows:
            target = members if row["permission"] == "edit" else viewers
            target.setdefault(row["note_id"], []).append(row["username"])
        result = []
        for row in rows:
            note = {
                "id": row["id"], "title": row["title"], "content": row["content"],
                "tag": row["tag"], "tagStyle": row["tag_style"],
                "time": row["time_label"], "owner": row["owner"],
                "members": members.get(row["id"], []),
                "viewers": viewers.get(row["id"], []),
            }
            result.append(note)
        return result

    def _user_ids(self, cursor):
        cursor.execute("SELECT id, username FROM users")
        return {row["username"]: row["id"] for row in cursor.fetchall()}

    def save_notes(self, notes):
        note_ids = {int(note["id"]) for note in notes}
        with self.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    user_ids = self._user_ids(cursor)
                    for note in notes:
                        owner_id = user_ids.get(note.get("owner"))
                        if owner_id is None:
                            raise StorageConfigError(f"Unknown note owner: {note.get('owner')!r}")
                        cursor.execute("""
                            INSERT INTO notes
                                (id, owner_id, title, content, tag, tag_style, time_label)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                owner_id=VALUES(owner_id), title=VALUES(title),
                                content=VALUES(content), tag=VALUES(tag),
                                tag_style=VALUES(tag_style), time_label=VALUES(time_label)
                        """, (
                            note["id"], owner_id, note.get("title", "未命名筆記"),
                            note.get("content", ""), note.get("tag", "草稿"),
                            note.get("tagStyle", ""), note.get("time", "剛剛"),
                        ))
                        cursor.execute("DELETE FROM note_members WHERE note_id=%s", (note["id"],))
                        member_values = []
                        for username in note.get("members", []):
                            if username in user_ids:
                                member_values.append((note["id"], user_ids[username], "edit"))
                        for username in note.get("viewers", []):
                            if username in user_ids:
                                member_values.append((note["id"], user_ids[username], "view"))
                        if member_values:
                            cursor.executemany("""
                                INSERT INTO note_members (note_id, user_id, permission)
                                VALUES (%s, %s, %s)
                            """, member_values)
                    if note_ids:
                        placeholders = ",".join(["%s"] * len(note_ids))
                        cursor.execute(f"DELETE FROM notes WHERE id NOT IN ({placeholders})", tuple(note_ids))
                    else:
                        cursor.execute("DELETE FROM notes")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def create_note(self, note):
        with self.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM users WHERE username=%s", (note["owner"],))
                    owner = cursor.fetchone()
                    if not owner:
                        raise StorageConfigError(f"Unknown note owner: {note['owner']!r}")
                    cursor.execute("""
                        INSERT INTO notes (owner_id, title, content, tag, tag_style, time_label)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        owner["id"], note.get("title", "未命名筆記"), note.get("content", ""),
                        note.get("tag", "草稿"), note.get("tagStyle", ""), note.get("time", "剛剛"),
                    ))
                    note_id = cursor.lastrowid
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {**note, "id": note_id}

    def close(self):
        pass


def mysql_config_from_env():
    database = os.environ.get("COLLABNOTE_DB_NAME", "collabnote").strip()
    user = os.environ.get("COLLABNOTE_DB_USER", "collabnote").strip()
    password = os.environ.get("COLLABNOTE_DB_PASSWORD", "")
    if not database or not user:
        raise StorageConfigError("COLLABNOTE_DB_NAME and COLLABNOTE_DB_USER cannot be empty")
    return {
        "host": os.environ.get("COLLABNOTE_DB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        "port": int(os.environ.get("COLLABNOTE_DB_PORT", "3306")),
        "user": user,
        "password": password,
        "database": database,
    }


def create_storage(base_dir, backend=None):
    name = (backend or os.environ.get("COLLABNOTE_STORAGE", "mysql")).strip().lower()
    if name == "mysql":
        return MySQLStorage()
    if name == "json":
        return JsonStorage(base_dir)
    raise StorageConfigError("COLLABNOTE_STORAGE must be 'mysql' or 'json'")
