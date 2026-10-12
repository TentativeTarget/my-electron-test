#!/usr/bin/env python3
"""Import users.json and notes.json into MySQL without deleting the JSON files.

Usage:
  COLLABNOTE_STORAGE=mysql python3 migrate_json_to_mysql.py --dry-run
  COLLABNOTE_STORAGE=mysql python3 migrate_json_to_mysql.py
"""
import argparse
import json
from pathlib import Path

from database import JsonStorage, MySQLStorage


def main():
    parser = argparse.ArgumentParser(description="Import CollabNote JSON data into MySQL")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--dry-run", action="store_true", help="validate and report only")
    args = parser.parse_args()

    source = JsonStorage(args.base_dir)
    users = source.load_users()
    notes = source.load_notes()
    usernames = {user.get("username") for user in users}
    note_ids = [note.get("id") for note in notes]
    errors = []
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
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 2
    print(f"users: {len(users)}, notes: {len(notes)}")
    if args.dry_run:
        print("dry-run: no database changes made")
        return 0
    target = MySQLStorage()
    target.initialize()
    target.save_users(users)
    target.save_notes(notes)
    print("import complete; source JSON files were left unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
