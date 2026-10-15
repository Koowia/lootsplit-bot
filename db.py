import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from config import DB_PATH


@contextmanager
def db_transaction():
    """Транзакция: всё или ничего"""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_run(sql, params=()):
    """Одна команда без возврата значений"""
    with db_transaction() as conn:
        conn.execute(sql, params)


def db_all(sql, params=()):
    """SELECT, возвращает все строки"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def db_init():
    with db_transaction() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS players (nick TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, last_pushed INTEGER NOT NULL DEFAULT 0)')
        conn.execute('CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, op_type TEXT, details TEXT, synced INTEGER NOT NULL DEFAULT 0)')
        conn.execute("INSERT OR IGNORE INTO sync_meta (key, value) VALUES ('source_id', ?)", (uuid.uuid4().hex,))


def get_player(nick):
    rows = db_all('SELECT nick, balance, last_pushed FROM players WHERE lower(nick) = lower(?)', (nick,))
    return rows[0] if rows else None


def add_log(author, op_type, details):
    with db_transaction() as conn:
        conn.execute(
            'INSERT INTO operations (date, author, op_type, details) VALUES (?, ?, ?, ?)',
            (datetime.now().strftime('%d.%m.%Y %H:%M'), author, op_type, details)
        )