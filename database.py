import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import load_config

config = load_config()
DB_PATH = Path(config.db_path)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        cur = conn.cursor()

        # Користувачі
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            """
        )

        # Налаштування користувачів
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                default_monitor_interval INTEGER,
                history_days INTEGER,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

        # Методи оплати (банки)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                binance_pay_type TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_methods_pay_type "
            "ON payment_methods(binance_pay_type);"
        )

        # Тригери
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,                  -- 'continuous', 'cross', 'one_time'
                common_threshold INTEGER NOT NULL,    -- 0/1: спільний чи окремі пороги
                condition_type TEXT NOT NULL,        -- 'ge','le','cross_up','cross_down'
                min_amount_uah REAL,
                max_amount_uah REAL,
                min_order_uah REAL,
                max_order_uah REAL,
                interval_sec INTEGER NOT NULL,
                repeat_enabled INTEGER NOT NULL DEFAULT 1,
                repeat_interval_sec INTEGER,         -- тільки для continuous
                status TEXT NOT NULL,                -- 'active','paused','done'
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_notified_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_triggers_user_status "
            "ON triggers(user_id, status);"
        )

        # Банки у тригерах
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trigger_banks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id INTEGER NOT NULL,
                payment_method_id INTEGER NOT NULL,
                use_separate_threshold INTEGER NOT NULL DEFAULT 0,
                threshold_price REAL,                -- окремий поріг, якщо є
                previous_price REAL,                 -- для режиму cross
                UNIQUE(trigger_id, payment_method_id),
                FOREIGN KEY(trigger_id) REFERENCES triggers(id) ON DELETE CASCADE,
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_trigger_banks_trigger "
            "ON trigger_banks(trigger_id);"
        )

        # Історія спрацювань/сповіщень
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id INTEGER NOT NULL,
                payment_method_id INTEGER,
                price REAL NOT NULL,
                condition_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(trigger_id, payment_method_id, created_at),
                FOREIGN KEY(trigger_id) REFERENCES triggers(id) ON DELETE CASCADE,
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_trigger_time "
            "ON notifications(trigger_id, payment_method_id, created_at);"
        )

        # Історія цін
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_method_id INTEGER,
                price REAL NOT NULL,
                total_ads INTEGER NOT NULL,
                checked_ads INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(payment_method_id) REFERENCES payment_methods(id)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_history_time "
            "ON price_history(created_at);"
        )

        # Журнал помилок джерела
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS source_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_errors_time "
            "ON source_errors(created_at);"
        )

        conn.commit()


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def get_or_create_user(chat_id: int, username: str | None, first_name: str | None, last_name: str | None) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE chat_id = ?;", (chat_id,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE users SET username=?, first_name=?, last_name=?, last_seen_at=? WHERE id=?;",
                (username, first_name, last_name, now_iso(), row["id"]),
            )
            return row["id"]
        cur.execute(
            """
            INSERT INTO users (chat_id, username, first_name, last_name, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (chat_id, username, first_name, last_name, now_iso(), now_iso()),
        )
        return cur.lastrowid


def upsert_payment_method(binance_pay_type: str, display_name: str | None = None) -> int:
    if not display_name:
        display_name = binance_pay_type
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, display_name FROM payment_methods WHERE binance_pay_type=?;",
            (binance_pay_type,),
        )
        row = cur.fetchone()
        if row:
            if row["display_name"] != display_name:
                cur.execute(
                    "UPDATE payment_methods SET display_name=? WHERE id=?;",
                    (display_name, row["id"]),
                )
            return row["id"]
        cur.execute(
            """
            INSERT INTO payment_methods (binance_pay_type, display_name, enabled)
            VALUES (?, ?, 1);
            """,
            (binance_pay_type, display_name),
        )
        return cur.lastrowid


def get_payment_methods(enabled_only: bool = True):
    with get_conn() as conn:
        cur = conn.cursor()
        if enabled_only:
            cur.execute(
                "SELECT * FROM payment_methods WHERE enabled=1 ORDER BY display_name ASC;"
            )
        else:
            cur.execute("SELECT * FROM payment_methods ORDER BY display_name ASC;")
        return cur.fetchall()


def set_payment_method_enabled(method_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE payment_methods SET enabled=? WHERE id=?;",
            (1 if enabled else 0, method_id),
        )


def create_trigger(
    user_id: int,
    name: str,
    mode: str,
    common_threshold: bool,
    condition_type: str,
    min_amount_uah: float | None,
    max_amount_uah: float | None,
    min_order_uah: float | None,
    max_order_uah: float | None,
    interval_sec: int,
    repeat_enabled: bool,
    repeat_interval_sec: int | None,
) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO triggers (
                user_id, name, mode, common_threshold, condition_type,
                min_amount_uah, max_amount_uah, min_order_uah, max_order_uah,
                interval_sec, repeat_enabled, repeat_interval_sec,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?);
            """,
            (
                user_id,
                name,
                mode,
                1 if common_threshold else 0,
                condition_type,
                min_amount_uah,
                max_amount_uah,
                min_order_uah,
                max_order_uah,
                interval_sec,
                1 if repeat_enabled else 0,
                repeat_interval_sec,
                now_iso(),
                now_iso(),
            ),
        )
        return cur.lastrowid


def update_trigger(trigger_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values())
    values.append(trigger_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE triggers SET {cols} WHERE id=?;", values)


def get_trigger(trigger_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM triggers WHERE id=?;", (trigger_id,))
        return cur.fetchone()


def get_user_triggers(user_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM triggers WHERE user_id=? ORDER BY created_at DESC;",
            (user_id,),
        )
        return cur.fetchall()


def delete_trigger(trigger_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM triggers WHERE id=?;", (trigger_id,))


def add_trigger_bank(
    trigger_id: int,
    payment_method_id: int,
    use_separate_threshold: bool,
    threshold_price: float | None,
) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO trigger_banks
                (trigger_id, payment_method_id, use_separate_threshold, threshold_price)
            VALUES (?, ?, ?, ?);
            """,
            (
                trigger_id,
                payment_method_id,
                1 if use_separate_threshold else 0,
                threshold_price,
            ),
        )
        return cur.lastrowid


def get_trigger_banks(trigger_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tb.*, pm.display_name, pm.binance_pay_type
            FROM trigger_banks tb
            JOIN payment_methods pm ON pm.id = tb.payment_method_id
            WHERE tb.trigger_id=?
            ORDER BY pm.display_name;
            """,
            (trigger_id,),
        )
        return cur.fetchall()


def set_trigger_bank_previous_price(trigger_id: int, pm_id: int, value: float | None):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE trigger_banks SET previous_price=?
            WHERE trigger_id=? AND payment_method_id=?;
            """,
            (value, trigger_id, pm_id),
        )


def log_notification(
    trigger_id: int,
    payment_method_id: int | None,
    price: float,
    condition_type: str,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO notifications
                (trigger_id, payment_method_id, price, condition_type, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (trigger_id, payment_method_id, price, condition_type, now_iso()),
        )


def get_last_notification_time(trigger_id: int) -> Optional[str]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT created_at
            FROM notifications
            WHERE trigger_id=?
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            (trigger_id,),
        )
        row = cur.fetchone()
        return row["created_at"] if row else None


def get_last_notification_time_for_bank(trigger_id: int, payment_method_id: int) -> Optional[str]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT created_at
            FROM notifications
            WHERE trigger_id=? AND payment_method_id=?
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            (trigger_id, payment_method_id),
        )
        row = cur.fetchone()
        return row["created_at"] if row else None


def log_price_history(
    payment_method_id: int | None, price: float, total_ads: int, checked_ads: int
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO price_history
                (payment_method_id, price, total_ads, checked_ads, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (payment_method_id, price, total_ads, checked_ads, now_iso()),
        )


def log_source_error(source: str, message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO source_errors(source, message, created_at)
            VALUES (?, ?, ?);
            """,
            (source, message, now_iso()),
        )


def get_price_history_last_24h():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ph.*, pm.display_name
            FROM price_history ph
            LEFT JOIN payment_methods pm ON pm.id = ph.payment_method_id
            WHERE ph.created_at >= datetime('now', '-1 day')
            ORDER BY ph.created_at ASC;
            """
        )
        return cur.fetchall()


def get_user_settings(user_id: int) -> dict:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM user_settings WHERE user_id=?;",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                "default_monitor_interval": None,
                "history_days": None,
                "notifications_enabled": 1,
            }
        return {
            "default_monitor_interval": row["default_monitor_interval"],
            "history_days": row["history_days"],
            "notifications_enabled": row["notifications_enabled"],
        }


def set_user_settings(
    user_id: int,
    default_monitor_interval: int | None = None,
    history_days: int | None = None,
    notifications_enabled: int | None = None,
) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM user_settings WHERE user_id=?;",
            (user_id,),
        )
        exists = cur.fetchone() is not None

        if exists:
            updates = []
            params = []
            if default_monitor_interval is not None:
                updates.append("default_monitor_interval=?")
                params.append(default_monitor_interval)
            if history_days is not None:
                updates.append("history_days=?")
                params.append(history_days)
            if notifications_enabled is not None:
                updates.append("notifications_enabled=?")
                params.append(notifications_enabled)

            if updates:
                params.append(user_id)
                cur.execute(
                    f"UPDATE user_settings SET {', '.join(updates)} WHERE user_id=?;",
                    params,
                )
        else:
            cur.execute(
                """
                INSERT INTO user_settings
                    (user_id, default_monitor_interval, history_days, notifications_enabled)
                VALUES (?, ?, ?, ?);
                """,
                (
                    user_id,
                    default_monitor_interval,
                    history_days,
                    notifications_enabled if notifications_enabled is not None else 1,
                ),
            )
