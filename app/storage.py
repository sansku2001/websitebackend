import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


class UpdateLimitReached(RuntimeError):
    pass


_database_path: Path | None = None
_upload_dir: Path | None = None


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    if _database_path is None:
        raise RuntimeError("Tracker storage has not been initialized.")
    connection = sqlite3.connect(_database_path, timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_tracker(database_path: Path, upload_dir: Path) -> None:
    global _database_path, _upload_dir
    _database_path = database_path
    _upload_dir = upload_dir
    _database_path.parent.mkdir(parents=True, exist_ok=True)
    _upload_dir.mkdir(parents=True, exist_ok=True)
    with _connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS updates (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 120),
            description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 1000),
            title_mr TEXT NOT NULL DEFAULT '',
            description_mr TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL CHECK(stage IN ('Resolution','Developer','Agreement','Approvals','Construction','Possession')),
            file_url TEXT,
            created_at TEXT NOT NULL
        )""")
        update_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(updates)").fetchall()
        }
        if "title_mr" not in update_columns:
            db.execute("ALTER TABLE updates ADD COLUMN title_mr TEXT NOT NULL DEFAULT ''")
        if "description_mr" not in update_columns:
            db.execute("ALTER TABLE updates ADD COLUMN description_mr TEXT NOT NULL DEFAULT ''")
        db.execute(
            "UPDATE updates SET title_mr=?, description_mr=? WHERE title=? AND title_mr=''",
            (
                "सदस्यांसोबत कराराचा मसुदा सामायिक केला",
                "नमुना अपडेट: वकिलांनी मसुदा पाठवला आहे. कृपया १५ ऑक्टोबरपर्यंत आपल्या सूचना सचिवांना पाठवा.",
                "Draft agreement shared with members",
            ),
        )
        db.execute("""CREATE TABLE IF NOT EXISTS tracker_summary (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            current_stage TEXT NOT NULL CHECK(current_stage IN ('Resolution','Developer','Agreement','Approvals','Construction','Possession')),
            members_signed INTEGER NOT NULL CHECK(members_signed >= 0),
            members_total INTEGER NOT NULL CHECK(members_total >= members_signed),
            next_meeting TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS updates_limit_guard
            BEFORE INSERT ON updates
            WHEN (SELECT COUNT(*) FROM updates) >= 15
            BEGIN SELECT RAISE(ABORT, 'UPDATE_LIMIT_REACHED'); END""")
        db.execute("""CREATE TABLE IF NOT EXISTS tracker_metadata (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            sample_data_seeded INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("INSERT OR IGNORE INTO tracker_metadata (id, sample_data_seeded) VALUES (1, 0)")
        db.execute("""INSERT OR IGNORE INTO tracker_summary
            (id, current_stage, members_signed, members_total, next_meeting)
            VALUES (1, 'Resolution', 0, 0, '')""")
        seeded = db.execute("SELECT sample_data_seeded FROM tracker_metadata WHERE id=1").fetchone()[0]
        if not seeded:
            if db.execute("SELECT COUNT(*) FROM updates").fetchone()[0] == 0:
                sample_id = str(uuid4())
                db.execute(
                    "INSERT INTO updates (id, title, description, title_mr, description_mr, stage, file_url, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        sample_id,
                        "Draft agreement shared with members",
                        "Sample update: The lawyer has circulated the draft. Please send your comments to the secretary by 15 Oct.",
                        "सदस्यांसोबत कराराचा मसुदा सामायिक केला",
                        "नमुना अपडेट: वकिलांनी मसुदा पाठवला आहे. कृपया १५ ऑक्टोबरपर्यंत आपल्या सूचना सचिवांना पाठवा.",
                        "Agreement",
                        "2026-10-08T12:00:00+00:00",
                    ),
                )
                db.execute("UPDATE tracker_summary SET current_stage='Agreement' WHERE id=1 AND current_stage='Resolution'")
            db.execute("UPDATE tracker_metadata SET sample_data_seeded=1 WHERE id=1")


def get_tracker(limit: int) -> dict:
    with _connect() as db:
        updates = db.execute(
            "SELECT id, title, description, title_mr, description_mr, stage, file_url, created_at FROM updates ORDER BY created_at DESC LIMIT ?",
            (min(limit, 15),),
        ).fetchall()
        total_count = db.execute("SELECT COUNT(*) FROM updates").fetchone()[0]
        summary = db.execute(
            "SELECT current_stage, members_signed, members_total, next_meeting FROM tracker_summary WHERE id=1"
        ).fetchone()
    return {"summary": dict(summary), "total_count": total_count, "updates": [dict(row) for row in updates]}


def create_update(values: dict) -> dict:
    update = {"id": str(uuid4()), **values, "created_at": datetime.now(timezone.utc).isoformat()}
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM updates").fetchone()[0] >= 15:
            raise UpdateLimitReached("Limit of 15 updates reached.")
        try:
            db.execute(
                "INSERT INTO updates (id, title, description, title_mr, description_mr, stage, file_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    update["id"], update["title"], update["description"],
                    update.get("title_mr", ""), update.get("description_mr", ""),
                    update["stage"], update["file_url"], update["created_at"],
                ),
            )
        except sqlite3.IntegrityError as error:
            if "UPDATE_LIMIT_REACHED" in str(error):
                raise UpdateLimitReached("Limit of 15 updates reached.") from error
            raise
    return update


def edit_update(update_id: str, values: dict) -> dict | None:
    with _connect() as db:
        result = db.execute(
            "UPDATE updates SET title=?, description=?, title_mr=?, description_mr=?, stage=?, file_url=? WHERE id=?",
            (
                values["title"], values["description"], values.get("title_mr", ""),
                values.get("description_mr", ""), values["stage"], values["file_url"], update_id,
            ),
        )
        if not result.rowcount:
            return None
        row = db.execute("SELECT * FROM updates WHERE id=?", (update_id,)).fetchone()
    return dict(row)


def delete_update(update_id: str) -> bool:
    with _connect() as db:
        result = db.execute("DELETE FROM updates WHERE id=?", (update_id,))
    return bool(result.rowcount)


def save_summary(values: dict) -> dict:
    with _connect() as db:
        db.execute("UPDATE tracker_summary SET current_stage=? WHERE id=1", (values["current_stage"],))
        row = db.execute("SELECT current_stage FROM tracker_summary WHERE id=1").fetchone()
    return dict(row)


def upload_file(data: bytes, content_type: str) -> dict[str, str]:
    extensions = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if _upload_dir is None:
        raise RuntimeError("Tracker storage has not been initialized.")
    name = f"{uuid4().hex}{extensions[content_type]}"
    (_upload_dir / name).write_bytes(data)
    return {"file_url": f"/uploads/{name}"}
