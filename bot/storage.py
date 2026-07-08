import sqlite3
from pathlib import Path

from bot.models import Application


class ApplicationStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    dealership TEXT NOT NULL,
                    status TEXT NOT NULL,
                    notified_status TEXT NOT NULL,
                    notified_conditions TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(applications)")
            }
            if "notified_conditions" not in columns:
                conn.execute(
                    "ALTER TABLE applications ADD COLUMN notified_conditions TEXT"
                )


            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(dealership_filters)")
            }
            if not existing_columns:
                conn.execute(
                    """
                    CREATE TABLE dealership_filters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dealership TEXT NOT NULL UNIQUE,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        chat_id TEXT,
                        contacts TEXT
                    )
                    """
                )
            elif "id" not in existing_columns:
                # Миграция со старой схемы (dealership TEXT PRIMARY KEY, enabled INTEGER).
                conn.execute("ALTER TABLE dealership_filters RENAME TO dealership_filters_old")
                conn.execute(
                    """
                    CREATE TABLE dealership_filters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dealership TEXT NOT NULL UNIQUE,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        chat_id TEXT,
                        contacts TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO dealership_filters (dealership, enabled) "
                    "SELECT dealership, enabled FROM dealership_filters_old"
                )
                conn.execute("DROP TABLE dealership_filters_old")
            else:
                if "chat_id" not in existing_columns:
                    conn.execute("ALTER TABLE dealership_filters ADD COLUMN chat_id TEXT")
                if "contacts" not in existing_columns:
                    conn.execute("ALTER TABLE dealership_filters ADD COLUMN contacts TEXT")

            # Одноразовые коды для привязки группы Telegram к салону (см. /link).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_links (
                    code TEXT PRIMARY KEY,
                    dealership_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            # Служебные значения бота (например, offset для Telegram getUpdates).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )


            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS application_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    dealership TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            event_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(application_events)")
            }
            if "percentage" not in event_columns:
                # Процентная ставка по условиям «Партнерский+» на момент события —
                # нужна для отчётов (средняя ставка по одобренным заявкам).
                conn.execute("ALTER TABLE application_events ADD COLUMN percentage REAL")
            if "loan_amount" not in event_columns:
                # Сумма кредита по условиям «Партнерский+» на момент события —
                # нужна для отчётов (средняя сумма кредита по одобренным заявкам).
                conn.execute("ALTER TABLE application_events ADD COLUMN loan_amount REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_created_at ON application_events(created_at)"
            )

    # --- заявки ---

    def get_notified_conditions(self, app_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notified_conditions FROM applications WHERE id = ?",
                (app_id,),
            ).fetchone()
            if not row:
                return None
            return row["notified_conditions"]

    def set_notified_conditions(self, app_id: str, fingerprint: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE applications SET notified_conditions = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (fingerprint, app_id),
            )

    def get_known_status(self, app_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notified_status FROM applications WHERE id = ?",
                (app_id,),
            ).fetchone()
            return row["notified_status"] if row else None

    def upsert(self, app: Application, notified_status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO applications (id, client_name, dealership, status, notified_status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    client_name = excluded.client_name,
                    dealership = excluded.dealership,
                    status = excluded.status,
                    notified_status = excluded.notified_status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (app.id, app.client_name, app.dealership, app.status, notified_status),
            )

    # --- фильтры по автосалонам ---

    def sync_dealerships(self, names: set[str]) -> dict[str, bool]:

        names = {n for n in names if n and n != "—"}
        if not names:
            return {}
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO dealership_filters (dealership, enabled) VALUES (?, 1) "
                "ON CONFLICT(dealership) DO NOTHING",
                [(name,) for name in names],
            )
            placeholders = ",".join("?" for _ in names)
            rows = conn.execute(
                f"SELECT dealership, enabled FROM dealership_filters WHERE dealership IN ({placeholders})",
                tuple(names),
            ).fetchall()
            return {row["dealership"]: bool(row["enabled"]) for row in rows}

    def register_dealership(self, dealership: str) -> None:
        """Добавляет автосалон в список известных (по умолчанию включён), если он ещё не встречался.

        Оставлено для точечного использования; в основном цикле применяйте sync_dealerships.
        """
        if not dealership or dealership == "—":
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO dealership_filters (dealership, enabled) VALUES (?, 1) "
                "ON CONFLICT(dealership) DO NOTHING",
                (dealership,),
            )

    def is_dealership_enabled(self, dealership: str) -> bool:
        """Неизвестные и пустые названия считаются включёнными по умолчанию."""
        if not dealership or dealership == "—":
            return True
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM dealership_filters WHERE dealership = ?",
                (dealership,),
            ).fetchone()
            if row is None:
                return True
            return bool(row["enabled"])

    def set_dealership_enabled(self, dealership: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO dealership_filters (dealership, enabled) VALUES (?, ?) "
                "ON CONFLICT(dealership) DO UPDATE SET enabled = excluded.enabled",
                (dealership, int(enabled)),
            )

    def set_all_dealerships_enabled(self, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE dealership_filters SET enabled = ?", (int(enabled),))

    def list_dealerships(self) -> list[tuple[int, str, bool]]:
        """Возвращает (id, название, включён) — id используется в callback_data кнопок."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, dealership, enabled FROM dealership_filters ORDER BY dealership COLLATE NOCASE"
            ).fetchall()
            return [(row["id"], row["dealership"], bool(row["enabled"])) for row in rows]

    def get_dealership_by_id(self, dealership_id: int) -> tuple[str, bool] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT dealership, enabled FROM dealership_filters WHERE id = ?",
                (dealership_id,),
            ).fetchone()
            if row is None:
                return None
            return row["dealership"], bool(row["enabled"])

    def list_enabled_dealership_names(self) -> set[str]:
        """Названия салонов, включённых в рассылку — для фильтрации отчётов."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dealership FROM dealership_filters WHERE enabled = 1"
            ).fetchall()
            return {row["dealership"] for row in rows}

    def set_dealership_enabled_by_id(self, dealership_id: int, enabled: bool) -> str | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE dealership_filters SET enabled = ? WHERE id = ?",
                (int(enabled), dealership_id),
            )
            row = conn.execute(
                "SELECT dealership FROM dealership_filters WHERE id = ?", (dealership_id,)
            ).fetchone()
            return row["dealership"] if row else None

    # --- контакты и привязанная группа Telegram ---

    def get_dealership_contacts(self, dealership_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT contacts FROM dealership_filters WHERE id = ?", (dealership_id,)
            ).fetchone()
            return row["contacts"] if row else None

    def set_dealership_contacts(self, dealership_id: int, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE dealership_filters SET contacts = ? WHERE id = ?", (text, dealership_id)
            )

    def get_dealership_chat_id(self, dealership_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id FROM dealership_filters WHERE id = ?", (dealership_id,)
            ).fetchone()
            return row["chat_id"] if row else None

    def set_dealership_chat_id(self, dealership_id: int, chat_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE dealership_filters SET chat_id = ? WHERE id = ?", (chat_id, dealership_id)
            )

    def get_dealership_chat_id_by_name(self, dealership: str) -> str | None:
        """Используется при отправке условий — по названию салона из заявки."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id FROM dealership_filters WHERE dealership = ?", (dealership,)
            ).fetchone()
            return row["chat_id"] if row and row["chat_id"] else None

    # --- одноразовые коды привязки группы (см. /link в telegram_commands.py) ---

    def create_link_code(self, dealership_id: int) -> str:
        import random
        import string

        from config import now_local

        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        created_at = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_links (code, dealership_id, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET dealership_id = excluded.dealership_id, "
                "created_at = excluded.created_at",
                (code, dealership_id, created_at),
            )
        return code

    def resolve_link_code(self, code: str, max_age_minutes: int = 30) -> int | None:
        """Возвращает dealership_id и удаляет код (одноразовый). None — если не найден/истёк."""
        from datetime import datetime, timedelta

        from config import now_local

        with self._connect() as conn:
            row = conn.execute(
                "SELECT dealership_id, created_at FROM pending_links WHERE code = ?", (code,)
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM pending_links WHERE code = ?", (code,))
            created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            if now_local() - created > timedelta(minutes=max_age_minutes):
                return None
            return row["dealership_id"]

    # --- ожидание текстового ввода (например, контактов) от админ-чата ---

    def set_awaiting_contact(self, chat_id: str, dealership_id: int) -> None:
        self.set_state(f"awaiting_contact:{chat_id}", str(dealership_id))

    def peek_awaiting_contact(self, chat_id: str) -> int | None:
        value = self.get_state(f"awaiting_contact:{chat_id}")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def clear_awaiting_contact(self, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM bot_state WHERE key = ?", (f"awaiting_contact:{chat_id}",))

    # --- статистика по конкретному салону (для карточки) ---

    def get_events_for_dealer(self, dealership_id: int) -> list[dict]:
        info = self.get_dealership_by_id(dealership_id)
        if not info:
            return []
        name, _enabled = info
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_type, status, percentage, loan_amount, created_at "
                "FROM application_events WHERE dealership = ?",
                (name,),
            ).fetchall()
            return [dict(row) for row in rows]

    # --- служебное состояние (Telegram offset и т.п.) ---

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bot_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_update_offset(self) -> int:
        value = self.get_state("tg_offset", "0")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def set_update_offset(self, offset: int) -> None:
        self.set_state("tg_offset", str(offset))

    # --- журнал событий (для отчётов) ---

    def log_event(
        self,
        app_id: str,
        dealership: str,
        client_name: str,
        event_type: str,
        status: str | None,
        percentage: float | None = None,
        loan_amount: float | None = None,
    ) -> None:

        from config import now_local

        created_at = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO application_events
                    (app_id, dealership, client_name, event_type, status, created_at, percentage, loan_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id, dealership or "—", client_name or "—", event_type, status or "",
                    created_at, percentage, loan_amount,
                ),
            )

    def get_events_missing_percentage(self) -> list[dict]:

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, app_id FROM application_events "
                "WHERE event_type = 'approval_conditions' AND percentage IS NULL"
            ).fetchall()
            return [dict(row) for row in rows]

    def update_event_percentage(self, event_id: int, percentage: float | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE application_events SET percentage = ? WHERE id = ?",
                (percentage, event_id),
            )

    def get_events_between(self, start: str, end: str) -> list[dict]:
        """start/end в формате 'YYYY-MM-DD HH:MM:SS', конец не включается."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT app_id, dealership, client_name, event_type, status, created_at, percentage, loan_amount
                FROM application_events
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at
                """,
                (start, end),
            ).fetchall()
            return [dict(row) for row in rows]
