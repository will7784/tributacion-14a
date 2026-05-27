import sqlite3
import os
from pathlib import Path
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

AUTH_DB = Path(os.environ.get("DATA_DIR", ".")) / "auth.db"


def _get_conn():
    return sqlite3.connect(AUTH_DB)


def init_auth_db():
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_superuser INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def create_user(username: str, password: str, is_superuser: bool = False):
    init_auth_db()
    conn = _get_conn()
    cursor = conn.cursor()
    pw_hash = generate_password_hash(password)
    cursor.execute(
        "INSERT OR REPLACE INTO users (username, password_hash, is_superuser) VALUES (?, ?, ?)",
        (username, pw_hash, 1 if is_superuser else 0),
    )
    conn.commit()
    conn.close()


def verify_user(username: str, password: str) -> bool:
    if not AUTH_DB.exists():
        return False
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT password_hash FROM users WHERE username = ?", (username,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False
    return check_password_hash(row[0], password)


def get_user(username: str) -> dict:
    if not AUTH_DB.exists():
        return None
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, is_superuser FROM users WHERE username = ?", (username,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "is_superuser": bool(row[2])}


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import session, redirect, url_for, request
        if "user" not in session:
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function
