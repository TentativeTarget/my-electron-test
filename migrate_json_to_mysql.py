#!/usr/bin/env python3
"""Import users.json, notes.json and ledgers.json into MySQL without deleting the JSON files.

Usage:
  COLLABNOTE_STORAGE=mysql python3 migrate_json_to_mysql.py --dry-run
  COLLABNOTE_STORAGE=mysql python3 migrate_json_to_mysql.py
"""
import argparse
import json
from pathlib import Path

from database import JsonStorage, MySQLStorage

LEDGER_ROLES = ("owner", "admin", "editor", "viewer")


def load_json_ledgers(base_dir):
    path = Path(base_dir) / "ledgers.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(f"ERROR: ledgers.json is not valid JSON: {error}")
    return data if isinstance(data, list) else []


def mysql_datetime(value):
    """把 JSON 里的 ISO 时间转成 MySQL DATETIME 能接受的字符串。"""
    if not value:
        return None
    text = str(value).strip().replace("T", " ").replace("Z", "")
    if "+" in text:
        text = text.split("+", 1)[0]
    return text[:19]


def validate(users, notes, ledgers):
    errors = []
    warnings = []
    usernames = {user.get("username") for user in users}
    note_ids = [note.get("id") for note in notes]
    if len(usernames) != len(users):
        errors.append("users.json contains duplicate or empty usernames")
    if len(set(note_ids)) != len(note_ids):
        errors.append("notes.json contains duplicate note ids")
    for note in notes:
        if note.get("owner") and note["owner"] not in usernames:
            errors.append(f"note {note.get('id')} has unknown owner {note.get('owner')!r}")
        for key in ("members", "viewers"):
            for name in note.get(key, []) or []:
                if name not in usernames:
                    errors.append(f"note {note.get('id')} has unknown {key} user {name!r}")

    ledger_ids = [ledger.get("id") for ledger in ledgers]
    if len(set(ledger_ids)) != len(ledger_ids):
        errors.append("ledgers.json contains duplicate ledger ids")
    for ledger in ledgers:
        ledger_id = ledger.get("id")
        owner = ledger.get("owner")
        if not owner:
            errors.append(f"ledger {ledger_id} has no owner")
        elif owner not in usernames:
            errors.append(f"ledger {ledger_id} has unknown owner {owner!r}")
        for name, role in (ledger.get("members") or {}).items():
            if name not in usernames:
                warnings.append(f"ledger {ledger_id}: skip member {name!r} (unknown user)")
            if role not in LEDGER_ROLES:
                warnings.append(f"ledger {ledger_id}: member {name!r} has invalid role {role!r}, will import as viewer")
        for txn in ledger.get("transactions") or []:
            author = txn.get("createdBy")
            if author and author not in usernames:
                warnings.append(f"ledger {ledger_id}: skip transaction {txn.get('id')} (unknown author {author!r})")
            payer = txn.get("paidBy")
            if payer and payer not in usernames:
                warnings.append(f"ledger {ledger_id}: transaction {txn.get('id')} payer {payer!r} not found, will be NULL")
    return errors, warnings


def import_ledgers(target, ledgers, usernames):
    """把 JSON 账本写进 MySQL，并维护旧 ID → 新 ID 映射，保证历史记录能正确还原。"""
    with target.connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, username FROM users")
                user_ids = {row["username"]: row["id"] for row in cursor.fetchall()}

                for ledger in ledgers:
                    owner_id = user_ids.get(ledger.get("owner"))
                    if owner_id is None:
                        continue
                    cursor.execute(
                        "INSERT INTO ledgers (owner_id, name, icon, budget, deleted_at) VALUES (%s, %s, %s, %s, %s)",
                        (
                            owner_id,
                            str(ledger.get("name") or "未命名账本")[:128],
                            str(ledger.get("icon") or "fa-book")[:64],
                            max(0.0, float(ledger.get("budget") or 0)),
                            mysql_datetime(ledger.get("deleted_at")),
                        ),
                    )
                    new_ledger_id = cursor.lastrowid

                    members = ledger.get("members") or {}
                    for username, role in members.items():
                        member_id = user_ids.get(username)
                        if member_id is None:
                            continue
                        cursor.execute(
                            "INSERT INTO ledger_members (ledger_id, user_id, role) VALUES (%s, %s, %s)",
                            (new_ledger_id, member_id, role if role in LEDGER_ROLES else "viewer"),
                        )

                    txn_id_map = {}
                    for txn in ledger.get("transactions") or []:
                        author_id = user_ids.get(txn.get("createdBy"))
                        if author_id is None:
                            continue
                        payer_id = user_ids.get(txn.get("paidBy"))
                        split = txn.get("splitMembers") or []
                        cursor.execute(
                            """
                            INSERT INTO transactions
                                (ledger_id, type, amount, category, note, created_by, paid_by,
                                 split_members, occurred_at, deleted_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                new_ledger_id,
                                "income" if txn.get("type") == "income" else "expense",
                                abs(float(txn.get("amount") or 0)),
                                str(txn.get("category") or "其他")[:32],
                                str(txn.get("note") or "")[:255],
                                author_id,
                                payer_id,
                                json.dumps(split, ensure_ascii=False),
                                mysql_datetime(txn.get("occurredAt")),
                                mysql_datetime(txn.get("deleted_at")),
                            ),
                        )
                        txn_id_map[int(txn.get("id") or 0)] = cursor.lastrowid

                    for entry in ledger.get("history") or []:
                        entity_type = entry.get("entityType") or "transaction"
                        entity_id = entry.get("entityId")
                        if entity_type == "transaction":
                            entity_id = txn_id_map.get(int(entity_id or 0))
                        actor_id = user_ids.get(entry.get("actor"))
                        snapshot = entry.get("snapshot")
                        cursor.execute(
                            """
                            INSERT INTO ledger_history
                                (ledger_id, action, entity_type, entity_id, snapshot, actor_id, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                new_ledger_id,
                                str(entry.get("action") or "update")[:32],
                                str(entity_type)[:32],
                                entity_id,
                                json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
                                actor_id,
                                mysql_datetime(entry.get("createdAt")),
                            ),
                        )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description="Import CollabNote JSON data into MySQL")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--dry-run", action="store_true", help="validate and report only")
    args = parser.parse_args()

    source = JsonStorage(args.base_dir)
    users = source.load_users()
    notes = source.load_notes()
    ledgers = load_json_ledgers(args.base_dir)

    errors, warnings = validate(users, notes, ledgers)
    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 2

    txn_count = sum(len(ledger.get("transactions") or []) for ledger in ledgers)
    print(f"users: {len(users)}, notes: {len(notes)}, ledgers: {len(ledgers)} ({txn_count} transactions)")
    if args.dry_run:
        print("dry-run: no database changes made")
        return 0

    target = MySQLStorage()
    target.initialize()
    target.save_users(users)
    target.save_notes(notes)
    if ledgers:
        import_ledgers(target, ledgers, {user.get("username") for user in users})
    print("import complete; source JSON files were left unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
