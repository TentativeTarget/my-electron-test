"""CollabNote storage backends.

MySQL is the production backend. JSON remains available only as an explicit
compatibility/fallback backend and for importing existing installations.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
import re
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


class JsonLedgerStore:
    """JSON 模式的账本存储，与 MySQL 版接口兼容，供 COLLABNOTE_STORAGE=json 使用。"""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.ledgers_file = self.base_dir / "ledgers.json"

    def _read(self):
        data = JsonStorage._read(self.ledgers_file)
        return data if isinstance(data, list) else []

    def _write(self, ledgers):
        JsonStorage._write(self.ledgers_file, ledgers)

    def _all(self):
        return self._read()

    def list_ledgers(self, username):
        result = []
        for ledger in self._all():
            if ledger.get("deleted_at"):
                continue
            role = (ledger.get("members") or {}).get(username)
            if not role:
                continue
            result.append({
                "id": ledger["id"], "name": ledger.get("name", ""),
                "icon": ledger.get("icon", "fa-book"), "budget": float(ledger.get("budget", 0) or 0),
                "owner": ledger.get("owner", ""), "role": role,
            })
        result.sort(key=lambda x: -x["id"])
        return result

    def create_ledger(self, owner, name, icon="fa-book", budget=0):
        ledgers = self._all()
        new_id = max([int(l.get("id", 0)) for l in ledgers] or [0]) + 1
        ledgers.append({
            "id": new_id, "name": str(name)[:128], "icon": str(icon)[:64] or "fa-book", "budget": max(0.0, float(budget or 0)),
            "owner": owner, "members": {owner: "owner"},
            "transactions": [], "history": [], "deleted_at": None,
        })
        self._write(ledgers)
        return new_id

    def _find(self, ledgers, ledger_id):
        return next((l for l in ledgers if int(l.get("id", 0)) == int(ledger_id)), None)

    def role_of(self, ledger_id, username):
        ledger = self._find(self._all(), ledger_id)
        if not ledger or ledger.get("deleted_at"):
            return None
        return (ledger.get("members") or {}).get(username)

    def ledger_exists(self, ledger_id):
        ledger = self._find(self._all(), ledger_id)
        return bool(ledger) and not ledger.get("deleted_at")

    def list_members(self, ledger_id):
        ledger = self._find(self._all(), ledger_id)
        if not ledger:
            return []
        return [{"username": u, "role": r} for u, r in (ledger.get("members") or {}).items()]

    def set_member_role(self, ledger_id, username, role):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            raise StorageConfigError(f"Unknown ledger: {ledger_id!r}")
        ledger.setdefault("members", {})[username] = role
        self._write(ledgers)

    def remove_member(self, ledger_id, username):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            return False
        members = ledger.get("members") or {}
        if members.get(username) in (None, "owner"):
            return False
        members.pop(username, None)
        self._write(ledgers)
        return True

    def list_transactions(self, ledger_id, include_deleted=False):
        ledger = self._find(self._all(), ledger_id)
        if not ledger:
            return []
        rows = ledger.get("transactions") or []
        if not include_deleted:
            rows = [t for t in rows if not t.get("deleted_at")]
        return list(reversed(rows))

    def add_transaction(self, ledger_id, username, txn_type, amount, category, note, paid_by=None, split_members=None):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            raise StorageConfigError(f"Unknown ledger: {ledger_id!r}")
        rows = ledger.setdefault("transactions", [])
        new_id = max([int(t.get("id", 0)) for t in rows] or [0]) + 1
        rows.append({
            "id": new_id, "type": txn_type, "amount": float(amount),
            "category": str(category)[:32] or "其他", "note": str(note)[:255], "createdBy": username,
            "paidBy": paid_by or username, "splitMembers": split_members or [],
            "occurredAt": datetime.now().isoformat(), "deleted_at": None,
        })
        self._write(ledgers)
        return new_id

    def update_transaction(self, ledger_id, txn_id, username, changes):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            return None
        for t in ledger.get("transactions") or []:
            if int(t.get("id", 0)) != int(txn_id) or t.get("deleted_at"):
                continue
            if "note" in changes:
                t["note"] = str(changes["note"])[:255]
            if "category" in changes:
                t["category"] = str(changes["category"])[:32] or "其他"
            if "type" in changes and changes["type"] in ("income", "expense"):
                t["type"] = changes["type"]
            if "amount" in changes:
                try:
                    amount = abs(float(changes["amount"]))
                except (TypeError, ValueError):
                    return None
                if amount <= 0:
                    return None
                t["amount"] = amount
            if "split_members" in changes:
                t["splitMembers"] = changes["split_members"] or []
            self._write(ledgers)
            return True
        return None

    def update_ledger(self, ledger_id, name=None, icon=None, budget=None):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            return False
        if name is not None:
            ledger["name"] = str(name)[:128]
        if icon is not None:
            ledger["icon"] = str(icon)[:64]
        if budget is not None:
            try:
                ledger["budget"] = max(0.0, float(budget))
            except (TypeError, ValueError):
                return False
        self._write(ledgers)
        return True

    def soft_delete_transaction(self, ledger_id, txn_id, username):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            return None
        for t in ledger.get("transactions") or []:
            if int(t.get("id", 0)) != int(txn_id) or t.get("deleted_at"):
                continue
            t["deleted_at"] = datetime.now().isoformat()
            snapshot = dict(t)
            ledger.setdefault("history", []).insert(0, {
                "id": len(ledger.get("history", [])) + 1, "action": "delete",
                "entityType": "transaction", "entityId": txn_id,
                "snapshot": snapshot, "actor": username, "createdAt": datetime.now().isoformat(),
            })
            self._write(ledgers)
            return snapshot
        return None

    def list_history(self, ledger_id):
        ledger = self._find(self._all(), ledger_id)
        return list(ledger.get("history") or []) if ledger else []

    def restore_transaction(self, ledger_id, history_id, username):
        ledgers = self._all()
        ledger = self._find(ledgers, ledger_id)
        if not ledger:
            return False
        entry = next((h for h in (ledger.get("history") or []) if int(h.get("id", 0)) == int(history_id)), None)
        if not entry:
            return False
        txn_id = entry.get("entityId")
        for t in ledger.get("transactions") or []:
            if int(t.get("id", 0)) == int(txn_id):
                t["deleted_at"] = None
                self._write(ledgers)
                return True
        return False

    def settlement(self, ledger_id, start=None, end=None):
        ledger = self._find(self._all(), ledger_id)
        if not ledger:
            return []
        net = {}
        def bump(name, delta):
            net[name] = round(net.get(name, 0) + delta, 2)
        for t in ledger.get("transactions") or []:
            if t.get("deleted_at") or t.get("type") != "expense":
                continue
            splits = t.get("splitMembers") or []
            if not splits:
                continue
            occurred = t.get("occurredAt") or ""
            if start and occurred[:10] < start:
                continue
            if end and occurred[:10] > end:
                continue
            payer = t.get("paidBy") or t.get("createdBy")
            share = float(t.get("amount", 0)) / len(splits)
            bump(payer, float(t.get("amount", 0)))
            for member in splits:
                bump(member, -share)
        creditors = sorted([[n, v] for n, v in net.items() if v > 0.01], key=lambda x: -x[1])
        debtors = sorted([[n, -v] for n, v in net.items() if v < -0.01], key=lambda x: -x[1])
        transfers = []
        i = j = 0
        while i < len(debtors) and j < len(creditors):
            debtor, owe = debtors[i]
            creditor, due = creditors[j]
            pay = round(min(owe, due), 2)
            if pay > 0:
                transfers.append({"from": debtor, "to": creditor, "amount": pay})
            debtors[i][1] = round(owe - pay, 2)
            creditors[j][1] = round(due - pay, 2)
            if debtors[i][1] <= 0.01: i += 1
            if creditors[j][1] <= 0.01: j += 1
        return transfers


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
        sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)  # 先剝掉註解，避免註解內分號切斷语句
        statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
        with self.connection() as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
                # 幂等迁移：为已存在的 transactions 表补上 AA 相关列
                for column_sql in (
                    "ADD COLUMN paid_by BIGINT UNSIGNED NULL",
                    "ADD COLUMN split_members JSON NULL",
                ):
                    try:
                        cursor.execute("ALTER TABLE transactions " + column_sql)
                    except Exception:
                        pass  # 列已存在则忽略
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


# ═══════════ 金融账本（按行操作，不整批读写） ═══════════
LEDGER_ROLES = ("owner", "admin", "editor", "viewer")


class LedgerStore:
    """账本、成员、交易、历史的按行存储。包一层 MySQLStorage 的连接管理。"""

    def __init__(self, storage):
        self.storage = storage

    def _uid(self, cursor, username):
        cursor.execute("SELECT id FROM users WHERE username=%s", (username,))
        row = cursor.fetchone()
        return row["id"] if row else None

    def _uname(self, cursor, user_id):
        cursor.execute("SELECT username FROM users WHERE id=%s", (user_id,))
        row = cursor.fetchone()
        return row["username"] if row else ""

    def list_ledgers(self, username):
        """返回该用户能看到（未被删除）的账本，含其角色。"""
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                uid = self._uid(cursor, username)
                if uid is None:
                    return []
                cursor.execute("""
                    SELECT l.id, l.name, l.icon, l.budget, l.owner_id, m.role
                    FROM ledgers l JOIN ledger_members m ON m.ledger_id = l.id
                    WHERE m.user_id = %s AND l.deleted_at IS NULL
                    ORDER BY l.id DESC
                """, (uid,))
                rows = cursor.fetchall()
                result = []
                for row in rows:
                    result.append({
                        "id": row["id"],
                        "name": row["name"],
                        "icon": row["icon"],
                        "budget": float(row["budget"] or 0),
                        "owner": self._uname(cursor, row["owner_id"]),
                        "role": row["role"],
                    })
                return result

    def create_ledger(self, owner, name, icon="fa-book", budget=0):
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, owner)
                    if uid is None:
                        raise StorageConfigError(f"Unknown ledger owner: {owner!r}")
                    cursor.execute(
                        "INSERT INTO ledgers (owner_id, name, icon, budget) VALUES (%s, %s, %s, %s)",
                        (uid, name, icon, budget),
                    )
                    ledger_id = cursor.lastrowid
                    cursor.execute(
                        "INSERT INTO ledger_members (ledger_id, user_id, role) VALUES (%s, %s, 'owner')",
                        (ledger_id, uid),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return ledger_id

    def update_ledger(self, ledger_id, name=None, icon=None, budget=None):
        fields, values = [], []
        if name is not None:
            fields.append("name=%s"); values.append(str(name)[:128])
        if icon is not None:
            fields.append("icon=%s"); values.append(str(icon)[:64])
        if budget is not None:
            try:
                amount = max(0.0, float(budget))
            except (TypeError, ValueError):
                return False
            fields.append("budget=%s"); values.append(amount)
        if not fields:
            return False
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE ledgers SET {', '.join(fields)} WHERE id=%s AND deleted_at IS NULL",
                        tuple(values + [ledger_id]),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def soft_delete_ledger(self, ledger_id, username):
        """软删除账本（保留数据，可由拥有者/管理恢复）。"""
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, username)
                    cursor.execute(
                        "UPDATE ledgers SET deleted_at=NOW(), deleted_by=%s WHERE id=%s AND deleted_at IS NULL",
                        (uid, ledger_id),
                    )
                    affected = cursor.rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return affected > 0

    def role_of(self, ledger_id, username):
        """返回用户在账本中的角色，看不到则 None。"""
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                uid = self._uid(cursor, username)
                if uid is None:
                    return None
                cursor.execute(
                    "SELECT role FROM ledger_members WHERE ledger_id=%s AND user_id=%s",
                    (ledger_id, uid),
                )
                row = cursor.fetchone()
                return row["role"] if row else None

    def ledger_exists(self, ledger_id):
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM ledgers WHERE id=%s AND deleted_at IS NULL", (ledger_id,))
                return cursor.fetchone() is not None

    def list_members(self, ledger_id):
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT m.user_id, m.role, u.username, u.display_name, u.title, u.avatar_file
                    FROM ledger_members m JOIN users u ON u.id = m.user_id
                    WHERE m.ledger_id = %s
                    ORDER BY FIELD(m.role, 'owner','admin','editor','viewer'), m.user_id
                """, (ledger_id,))
                rows = cursor.fetchall()
                return [{
                    "username": row["username"],
                    "role": row["role"],
                    "displayName": row["display_name"] or row["username"],
                    "title": row["title"] or "团队成员",
                    "avatarUrl": f"/api/users/{row['username']}/avatar" if row["avatar_file"] else None,
                } for row in rows]

    def set_member_role(self, ledger_id, username, role):
        if role not in LEDGER_ROLES:
            raise StorageConfigError(f"Unknown ledger role: {role!r}")
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, username)
                    if uid is None:
                        raise StorageConfigError(f"Unknown user: {username!r}")
                    cursor.execute("""
                        INSERT INTO ledger_members (ledger_id, user_id, role)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE role=VALUES(role)
                    """, (ledger_id, uid, role))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def remove_member(self, ledger_id, username):
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, username)
                    if uid is None:
                        return False
                    cursor.execute(
                        "DELETE FROM ledger_members WHERE ledger_id=%s AND user_id=%s AND role <> 'owner'",
                        (ledger_id, uid),
                    )
                    affected = cursor.rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return affected > 0

    def list_transactions(self, ledger_id, include_deleted=False):
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                where = "t.ledger_id=%s" if include_deleted else "t.ledger_id=%s AND t.deleted_at IS NULL"
                cursor.execute(f"""
                    SELECT t.id, t.type, t.amount, t.category, t.note, t.occurred_at,
                           t.created_by, u.username AS created_name,
                           payer.username AS paid_by_name, t.split_members, t.deleted_at
                    FROM transactions t
                    JOIN users u ON u.id = t.created_by
                    LEFT JOIN users payer ON payer.id = t.paid_by
                    WHERE {where}
                    ORDER BY t.occurred_at DESC, t.id DESC
                """, (ledger_id,))
                rows = cursor.fetchall()
                result = []
                for row in rows:
                    splits = row["split_members"]
                    if isinstance(splits, str):
                        try:
                            splits = json.loads(splits)
                        except (ValueError, TypeError):
                            splits = None
                    result.append({
                        "id": row["id"],
                        "type": row["type"],
                        "amount": float(row["amount"]),
                        "category": row["category"],
                        "note": row["note"],
                        "occurredAt": row["occurred_at"].isoformat() if row["occurred_at"] else None,
                        "createdBy": row["created_name"],
                        "paidBy": row["paid_by_name"] or row["created_name"],
                        "splitMembers": splits or [],
                        "deleted": row["deleted_at"] is not None,
                    })
                return result

    def add_transaction(self, ledger_id, username, txn_type, amount, category, note, paid_by=None, split_members=None):
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, username)
                    if uid is None:
                        raise StorageConfigError(f"Unknown user: {username!r}")
                    payer_id = self._uid(cursor, paid_by) if paid_by else uid
                    if payer_id is None:
                        payer_id = uid
                    splits_json = json.dumps(split_members, ensure_ascii=False) if split_members else None
                    cursor.execute("""
                        INSERT INTO transactions (ledger_id, type, amount, category, note, created_by, paid_by, split_members)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (ledger_id, txn_type, amount, category, note, uid, payer_id, splits_json))
                    txn_id = cursor.lastrowid
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return txn_id

    def settlement(self, ledger_id, start=None, end=None):
        """根据带 AA 分摊的支出，算出每人净额并简化为最少转账。
        start/end 为 ISO 日期字符串（含当天）。"""
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, username FROM users")
                name_by_id = {row["id"]: row["username"] for row in cursor.fetchall()}
                clauses = ["t.ledger_id=%s", "t.deleted_at IS NULL", "t.type='expense'", "t.split_members IS NOT NULL"]
                params = [ledger_id]
                if start:
                    clauses.append("t.occurred_at >= %s"); params.append(start + " 00:00:00")
                if end:
                    clauses.append("t.occurred_at <= %s"); params.append(end + " 23:59:59")
                cursor.execute(f"""
                    SELECT t.amount, t.paid_by, t.split_members
                    FROM transactions t WHERE {' AND '.join(clauses)}
                """, tuple(params))
                rows = cursor.fetchall()
        # 净额：正数表示对方该收（垫付多了），负数表示该付
        net = {}
        def bump(name, delta):
            net[name] = round(net.get(name, 0) + delta, 2)
        for row in rows:
            payer = name_by_id.get(row["paid_by"])
            splits = row["split_members"]
            if isinstance(splits, str):
                try:
                    splits = json.loads(splits)
                except (ValueError, TypeError):
                    splits = None
            if not payer or not splits:
                continue
            members = [m for m in splits if m in name_by_id.values()]
            if not members:
                continue
            share = float(row["amount"]) / len(members)
            bump(payer, float(row["amount"]))
            for member in members:
                bump(member, -share)
        # 简化转账：收的人从付的人那里拿
        creditors = sorted([[n, v] for n, v in net.items() if v > 0.01], key=lambda x: -x[1])
        debtors = sorted([[n, -v] for n, v in net.items() if v < -0.01], key=lambda x: -x[1])
        transfers = []
        i = j = 0
        while i < len(debtors) and j < len(creditors):
            debtor, owe = debtors[i]
            creditor, due = creditors[j]
            pay = round(min(owe, due), 2)
            if pay > 0:
                transfers.append({"from": debtor, "to": creditor, "amount": pay})
            debtors[i][1] = round(owe - pay, 2)
            creditors[j][1] = round(due - pay, 2)
            if debtors[i][1] <= 0.01: i += 1
            if creditors[j][1] <= 0.01: j += 1
        return transfers

    def update_transaction(self, ledger_id, txn_id, username, changes):
        """更新交易的内容/金额/类别/收支；返回 True，找不到则 None。"""
        fields, values = [], []
        if "note" in changes:
            fields.append("note=%s"); values.append(str(changes["note"])[:255])
        if "category" in changes:
            fields.append("category=%s"); values.append(str(changes["category"])[:32] or "其他")
        if "type" in changes and changes["type"] in ("income", "expense"):
            fields.append("type=%s"); values.append(changes["type"])
        if "amount" in changes:
            try:
                amount = abs(float(changes["amount"]))
            except (TypeError, ValueError):
                return None
            if amount <= 0:
                return None
            fields.append("amount=%s"); values.append(amount)
        if "split_members" in changes:
            splits = changes["split_members"]
            fields.append("split_members=%s")
            values.append(json.dumps(splits, ensure_ascii=False) if splits else None)
        if not fields:
            return None
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM transactions WHERE id=%s AND ledger_id=%s AND deleted_at IS NULL",
                        (txn_id, ledger_id),
                    )
                    if not cursor.fetchone():
                        return None
                    cursor.execute(
                        f"UPDATE transactions SET {', '.join(fields)} WHERE id=%s AND ledger_id=%s",
                        tuple(values + [txn_id, ledger_id]),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def soft_delete_transaction(self, ledger_id, txn_id, username):
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, username)
                    cursor.execute("""
                        SELECT id, ledger_id, type, amount, category, note, created_by, occurred_at,
                               paid_by, split_members
                        FROM transactions WHERE id=%s AND ledger_id=%s AND deleted_at IS NULL
                    """, (txn_id, ledger_id))
                    row = cursor.fetchone()
                    if not row:
                        return None
                    cursor.execute(
                        "UPDATE transactions SET deleted_at=NOW(), deleted_by=%s WHERE id=%s",
                        (uid, txn_id),
                    )
                    splits = row["split_members"]
                    if isinstance(splits, str):
                        try:
                            splits = json.loads(splits)
                        except (ValueError, TypeError):
                            splits = None
                    snapshot = {
                        "id": row["id"], "type": row["type"], "amount": float(row["amount"]),
                        "category": row["category"], "note": row["note"],
                        "paid_by": row["paid_by"], "split_members": splits or [],
                        "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
                    }
                    cursor.execute("""
                        INSERT INTO ledger_history (ledger_id, action, entity_type, entity_id, snapshot, actor_id)
                        VALUES (%s, 'delete', 'transaction', %s, %s, %s)
                    """, (ledger_id, txn_id, json.dumps(snapshot, ensure_ascii=False), uid))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return snapshot

    def list_history(self, ledger_id):
        with self.storage.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT h.id, h.action, h.entity_type, h.entity_id, h.snapshot, h.created_at,
                           u.username AS actor
                    FROM ledger_history h LEFT JOIN users u ON u.id = h.actor_id
                    WHERE h.ledger_id = %s
                    ORDER BY h.id DESC
                """, (ledger_id,))
                rows = cursor.fetchall()
                result = []
                for row in rows:
                    snap = row["snapshot"]
                    if isinstance(snap, str):
                        try:
                            snap = json.loads(snap)
                        except (ValueError, TypeError):
                            snap = None
                    result.append({
                        "id": row["id"],
                        "action": row["action"],
                        "entityType": row["entity_type"],
                        "entityId": row["entity_id"],
                        "snapshot": snap,
                        "actor": row["actor"] or "",
                        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
                    })
                return result

    def restore_transaction(self, ledger_id, history_id, username):
        """把某条历史记录对应的交易恢复回来（清空 deleted_at）。"""
        with self.storage.connection() as connection:
            try:
                with connection.cursor() as cursor:
                    uid = self._uid(cursor, username)
                    cursor.execute(
                        "SELECT entity_id FROM ledger_history WHERE id=%s AND ledger_id=%s AND action='delete'",
                        (history_id, ledger_id),
                    )
                    row = cursor.fetchone()
                    if not row or row["entity_id"] is None:
                        return False
                    cursor.execute(
                        "UPDATE transactions SET deleted_at=NULL, deleted_by=NULL WHERE id=%s AND ledger_id=%s",
                        (row["entity_id"], ledger_id),
                    )
                    restored = cursor.rowcount
                    cursor.execute("""
                        INSERT INTO ledger_history (ledger_id, action, entity_type, entity_id, actor_id)
                        VALUES (%s, 'restore', 'transaction', %s, %s)
                    """, (ledger_id, row["entity_id"], uid))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return restored > 0


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


def create_ledger_store(storage):
    """按存储类型返回对应的账本存储。JSON 模式不能直接用 MySQL 版。"""
    if isinstance(storage, MySQLStorage):
        return LedgerStore(storage)
    if isinstance(storage, JsonStorage):
        return JsonLedgerStore(storage.base_dir)
    raise StorageConfigError("Unknown storage backend for ledger store")
