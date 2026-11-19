"""SQLite ledger: cases, failed turns, batches, items, tasks, assets, events, reports."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .fixtures import PROJECT, P_BASE, failed_turn_id

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DEMO_DB", ROOT / "data" / "demo.sqlite3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT, domain TEXT
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, external_session_id TEXT UNIQUE, title TEXT, source TEXT,
  turns_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failed_turns (
  id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, case_id TEXT NOT NULL,
  turn_index INTEGER NOT NULL, score_name TEXT, score_value REAL, reason TEXT,
  replay_key TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, cycle TEXT, status TEXT, stop_reason TEXT,
  max_attempts INTEGER, verify_trials INTEGER,
  asset_json TEXT, counts_json TEXT, created_at TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
  id TEXT PRIMARY KEY, batch_id TEXT, failed_turn_id TEXT, state TEXT,
  attempts INTEGER DEFAULT 0, error_history TEXT, verification_json TEXT,
  changes_json TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, batch_id TEXT, failed_turn_id TEXT, attempt INTEGER,
  request_body TEXT, response_body TEXT, status TEXT, polls INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, seq INTEGER, content TEXT,
  content_hash TEXT, origin TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, kind TEXT, failed_turn_id TEXT,
  message TEXT, data_json TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  batch_id TEXT PRIMARY KEY, payload_json TEXT, created_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init() -> None:
    with conn() as db:
        db.executescript(SCHEMA)


def _insert_project():
    with conn() as db:
        db.execute(
            "INSERT OR IGNORE INTO projects(id, name, domain) VALUES (?, ?, ?)",
            (PROJECT["id"], PROJECT["name"], PROJECT["domain"]),
        )


def project() -> dict:
    with conn() as db:
        row = db.execute("SELECT * FROM projects LIMIT 1").fetchone()
    return dict(row) if row else dict(PROJECT)


def has_cases() -> bool:
    with conn() as db:
        return db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] > 0


def import_sessions(payload: dict) -> dict:
    """Idempotent: fingerprints decide identity; existing rows are never overwritten."""
    _insert_project()
    new_cases = new_turns = 0
    with conn() as db:
        for session in payload.get("sessions", []):
            external_id = session["session_id"]
            case_id = "case_" + external_id.replace("-", "")
            created = db.execute(
                "INSERT OR IGNORE INTO cases(id, external_session_id, title, source, turns_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    external_id,
                    session.get("title", external_id),
                    session.get("source", "unknown"),
                    json.dumps(session["turns"], ensure_ascii=False),
                    now(),
                ),
            ).rowcount
            new_cases += created
            for turn in session["turns"]:
                score = turn.get("score") or {}
                if score.get("value") != 0:
                    continue
                fingerprint = hashlib.sha256(
                    "\x1f".join(
                        [
                            external_id,
                            str(turn["index"]),
                            str(score.get("name")),
                            str(turn.get("user", "")),
                        ]
                    ).encode("utf-8")
                ).hexdigest()[:16]
                inserted = db.execute(
                    "INSERT OR IGNORE INTO failed_turns("
                    " id, fingerprint, case_id, turn_index, score_name, score_value, reason, replay_key, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        failed_turn_id(external_id, turn["index"]),
                        fingerprint,
                        case_id,
                        turn["index"],
                        score.get("name"),
                        score.get("value"),
                        score.get("reason", ""),
                        turn.get("replay_key"),
                        now(),
                    ),
                ).rowcount
                new_turns += inserted
    return {"cases_new": new_cases, "failed_turns_new": new_turns}


def list_failed_turns() -> list[dict]:
    with conn() as db:
        rows = db.execute(
            "SELECT f.*, c.title AS case_title, c.source AS case_source,"
            " c.external_session_id AS session_id"
            " FROM failed_turns f JOIN cases c ON c.id = f.case_id"
            " ORDER BY c.external_session_id, f.turn_index"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["replayable"] = bool(item.get("replay_key"))
        result.append(item)
    return result


def get_failed_turn(ft_id: str) -> dict | None:
    with conn() as db:
        row = db.execute(
            "SELECT f.*, c.title AS case_title, c.source AS case_source,"
            " c.turns_json AS case_turns_json, c.external_session_id AS session_id"
            " FROM failed_turns f JOIN cases c ON c.id = f.case_id WHERE f.id = ?",
            (ft_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["replayable"] = bool(item.get("replay_key"))
    item["case_turns"] = json.loads(item.pop("case_turns_json"))
    return item


def _counts(batch_id: str, db) -> dict:
    rows = db.execute(
        "SELECT state, COUNT(*) AS n FROM batch_items WHERE batch_id = ? GROUP BY state",
        (batch_id,),
    ).fetchall()
    counts = {
        "total": 0,
        "queued": 0,
        "skipped": 0,
        "passed": 0,
        "passed_incidentally": 0,
        "budget_exhausted": 0,
    }
    for row in rows:
        counts[row["state"]] = row["n"]
        counts["total"] += row["n"]
    return counts


def create_batch(cycle: str, failed_turn_ids: list[str], max_attempts: int = 3, verify_trials: int = 3) -> str:
    batch_id = "batch_" + uuid.uuid4().hex[:8]
    asset = {"main_template": P_BASE, "seq": 1, "hash": content_hash(P_BASE)}
    with conn() as db:
        db.execute(
            "INSERT INTO batches(id, cycle, status, stop_reason, max_attempts, verify_trials,"
            " asset_json, counts_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                cycle,
                "frozen",
                None,
                max_attempts,
                verify_trials,
                json.dumps(asset, ensure_ascii=False),
                "{}",
                now(),
            ),
        )
        db.execute(
            "INSERT INTO assets(batch_id, seq, content, content_hash, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, 1, P_BASE, asset["hash"], "baseline", now()),
        )
        for ft_id in failed_turn_ids:
            row = db.execute("SELECT * FROM failed_turns WHERE id = ?", (ft_id,)).fetchone()
            if not row:
                continue
            state = "queued" if row["replay_key"] else "skipped"
            db.execute(
                "INSERT INTO batch_items(id, batch_id, failed_turn_id, state, attempts,"
                " error_history, verification_json, changes_json, updated_at)"
                " VALUES (?, ?, ?, ?, 0, '[]', '[]', '[]', ?)",
                (f"item_{uuid.uuid4().hex[:8]}", batch_id, ft_id, state, now()),
            )
            if state == "skipped":
                add_event(
                    batch_id,
                    "item.skipped",
                    "无回放主键，冻结阶段跳过（永久不可自动验证）",
                    failed_turn_id=ft_id,
                    data={"reason": "no_replay_key"},
                    db=db,
                )
        add_event(batch_id, "batch.created", f"创建修复批次：{cycle}", db=db)
        queued = db.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id = ? AND state = 'queued'",
            (batch_id,),
        ).fetchone()[0]
        add_event(
            batch_id,
            "batch.frozen",
            f"冻结完成：{queued} 条进入队列，资产从 seq 1 开始",
            data={"queued": queued},
            db=db,
        )
        counts = _counts(batch_id, db)
        db.execute("UPDATE batches SET counts_json = ? WHERE id = ?", (json.dumps(counts), batch_id))
    return batch_id


def list_batches() -> list[dict]:
    with conn() as db:
        rows = db.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["asset"] = json.loads(item.pop("asset_json"))
        item["counts"] = json.loads(item.pop("counts_json") or "{}")
        result.append(item)
    return result


def get_batch(batch_id: str) -> dict | None:
    with conn() as db:
        row = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["asset"] = json.loads(item.pop("asset_json"))
    item["counts"] = json.loads(item.pop("counts_json") or "{}")
    return item


def batch_items(batch_id: str) -> list[dict]:
    with conn() as db:
        rows = db.execute(
            "SELECT i.*, f.turn_index, f.reason, f.replay_key, f.score_name, f.score_value,"
            " c.title AS case_title, c.external_session_id AS session_id"
            " FROM batch_items i"
            " JOIN failed_turns f ON f.id = i.failed_turn_id"
            " JOIN cases c ON c.id = f.case_id"
            " WHERE i.batch_id = ? ORDER BY c.external_session_id, f.turn_index",
            (batch_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["error_history"] = json.loads(item.get("error_history") or "[]")
        item["verification"] = json.loads(item.get("verification_json") or "[]")
        item["changes"] = json.loads(item.get("changes_json") or "[]")
        item["replayable"] = bool(item.get("replay_key"))
        result.append(item)
    return result


def update_batch(batch_id: str, **fields) -> None:
    allowed = {"status", "stop_reason", "asset_json", "counts_json", "started_at", "ended_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    with conn() as db:
        if "asset_json" in updates and not isinstance(updates["asset_json"], str):
            updates["asset_json"] = json.dumps(updates["asset_json"], ensure_ascii=False)
        if "counts_json" in updates and not isinstance(updates["counts_json"], str):
            updates["counts_json"] = json.dumps(updates["counts_json"], ensure_ascii=False)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE batches SET {assignments} WHERE id = ?", (*updates.values(), batch_id))


def refresh_counts(batch_id: str) -> dict:
    with conn() as db:
        counts = _counts(batch_id, db)
        db.execute(
            "UPDATE batches SET counts_json = ? WHERE id = ?",
            (json.dumps(counts), batch_id),
        )
    return counts


def update_item(item_id: str, **fields) -> None:
    allowed = {
        "state",
        "attempts",
        "error_history",
        "verification_json",
        "changes_json",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    for key in ("error_history", "verification_json", "changes_json"):
        if key in updates and not isinstance(updates[key], str):
            updates[key] = json.dumps(updates[key], ensure_ascii=False)
    updates["updated_at"] = now()
    with conn() as db:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE batch_items SET {assignments} WHERE id = ?", (*updates.values(), item_id))


def add_event(
    batch_id: str,
    kind: str,
    message: str,
    failed_turn_id: str | None = None,
    data: dict | None = None,
    db=None,
) -> int:
    params = (
        batch_id,
        kind,
        failed_turn_id,
        message,
        json.dumps(data or {}, ensure_ascii=False),
        now(),
    )
    if db is not None:
        return db.execute(
            "INSERT INTO events(batch_id, kind, failed_turn_id, message, data_json, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            params,
        ).lastrowid
    with conn() as own:
        return own.execute(
            "INSERT INTO events(batch_id, kind, failed_turn_id, message, data_json, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            params,
        ).lastrowid


def events(batch_id: str, after_id: int = 0) -> list[dict]:
    with conn() as db:
        rows = db.execute(
            "SELECT * FROM events WHERE batch_id = ? AND id > ? ORDER BY id",
            (batch_id, after_id),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item.pop("data_json") or "{}")
        result.append(item)
    return result


def add_asset(batch_id: str, seq: int, content: str, origin: str) -> None:
    with conn() as db:
        db.execute(
            "INSERT INTO assets(batch_id, seq, content, content_hash, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, seq, content, content_hash(content), origin, now()),
        )


def assets(batch_id: str) -> list[dict]:
    with conn() as db:
        rows = db.execute(
            "SELECT seq, content, content_hash, origin, created_at FROM assets"
            " WHERE batch_id = ? ORDER BY seq",
            (batch_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_task(
    task_id: str,
    batch_id: str,
    ft_id: str,
    attempt: int,
    request_body: dict,
    response_body: dict,
    status: str,
    polls: int,
) -> None:
    with conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO tasks(id, batch_id, failed_turn_id, attempt,"
            " request_body, response_body, status, polls, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                batch_id,
                ft_id,
                attempt,
                json.dumps(request_body, ensure_ascii=False),
                json.dumps(response_body, ensure_ascii=False),
                status,
                polls,
                now(),
            ),
        )


def all_replayable_turns() -> list[dict]:
    """Every turn with a replay key, across all cases — the regression scope."""
    result = []
    with conn() as db:
        rows = db.execute(
            "SELECT id, external_session_id, title, turns_json FROM cases ORDER BY external_session_id"
        ).fetchall()
    for row in rows:
        for turn in json.loads(row["turns_json"]):
            if turn.get("replay_key"):
                result.append(
                    {
                        "session_id": row["external_session_id"],
                        "case_title": row["title"],
                        "turn_index": turn["index"],
                        "request_id": turn["replay_key"],
                    }
                )
    return result


def save_report(batch_id: str, payload: dict) -> None:
    with conn() as db:
        db.execute(
            "INSERT OR REPLACE INTO reports(batch_id, payload_json, created_at) VALUES (?, ?, ?)",
            (batch_id, json.dumps(payload, ensure_ascii=False), now()),
        )


def get_report(batch_id: str) -> dict | None:
    with conn() as db:
        row = db.execute("SELECT payload_json FROM reports WHERE batch_id = ?", (batch_id,)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def overview() -> dict:
    with conn() as db:
        cases = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        failed = db.execute("SELECT COUNT(*) FROM failed_turns").fetchone()[0]
        replayable = db.execute(
            "SELECT COUNT(*) FROM failed_turns WHERE replay_key IS NOT NULL"
        ).fetchone()[0]
        batches = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    return {
        "project": project(),
        "counts": {
            "cases": cases,
            "failed_turns": failed,
            "replayable": replayable,
            "unreplayable": failed - replayable,
            "batches": batches,
        },
    }
