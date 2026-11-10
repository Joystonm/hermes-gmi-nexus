"""Persistent storage for execution history, benchmarks and routing statistics.

Inside Hermes the database lives at ``<hermes home>/plugin-data/gmi-nexus/data.db``
through Hermes' own ``plugins.plugin_storage`` helper, so it survives plugin
updates and follows the active profile. A standalone fallback is used when
the plugin is exercised outside Hermes (unit tests, ``python -m nexus``).

Nothing sensitive is stored: model ids, timings, token counts, estimated
costs and (optionally) truncated output excerpts only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

#: The plugin's storage namespace. Must match the plugin's manifest name.
PLUGIN_NAME = "gmi-nexus"

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        id            TEXT PRIMARY KEY,
        goal          TEXT NOT NULL,
        task_type     TEXT NOT NULL,
        status        TEXT NOT NULL,
        created_at    REAL NOT NULL,
        finished_at   REAL,
        total_cost_usd REAL NOT NULL DEFAULT 0,
        total_latency_ms INTEGER NOT NULL DEFAULT 0,
        models_used   TEXT NOT NULL DEFAULT '[]',
        report        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS steps (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        description   TEXT NOT NULL,
        task_type     TEXT NOT NULL,
        status        TEXT NOT NULL,
        model_id      TEXT,
        output        TEXT,
        media_urls    TEXT NOT NULL DEFAULT '[]',
        latency_ms    INTEGER NOT NULL DEFAULT 0,
        attempts      INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd      REAL NOT NULL DEFAULT 0,
        error         TEXT,
        validation    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_stats (
        model_id        TEXT NOT NULL,
        task_type       TEXT NOT NULL,
        runs            INTEGER NOT NULL DEFAULT 0,
        successes       INTEGER NOT NULL DEFAULT 0,
        failures        INTEGER NOT NULL DEFAULT 0,
        ewma_latency_ms REAL NOT NULL DEFAULT 0,
        ewma_cost_usd   REAL NOT NULL DEFAULT 0,
        total_cost_usd  REAL NOT NULL DEFAULT 0,
        last_used_at    REAL,
        PRIMARY KEY (model_id, task_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS benchmarks (
        id          TEXT PRIMARY KEY,
        suite       TEXT NOT NULL,
        created_at  REAL NOT NULL,
        models      TEXT NOT NULL DEFAULT '[]',
        status      TEXT NOT NULL DEFAULT 'running',
        summary     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS benchmark_results (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        benchmark_id  TEXT NOT NULL,
        model_id      TEXT NOT NULL,
        task_index    INTEGER NOT NULL DEFAULT 0,
        task          TEXT NOT NULL,
        latency_ms    INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd      REAL NOT NULL DEFAULT 0,
        score         REAL,
        passed        INTEGER,
        output        TEXT,
        error         TEXT,
        created_at    REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS capability_probes (
        model_id    TEXT NOT NULL,
        capability  TEXT NOT NULL,
        verified    INTEGER NOT NULL,
        evidence    TEXT,
        probed_at   REAL NOT NULL,
        PRIMARY KEY (model_id, capability)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL NOT NULL,
        kind        TEXT NOT NULL,
        model_id    TEXT,
        cost_usd    REAL NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        requests    INTEGER NOT NULL DEFAULT 0,
        detail      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_requests (
        request_id    TEXT PRIMARY KEY,
        model_id      TEXT NOT NULL,
        payload_hash  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'submitted',
        media_urls    TEXT NOT NULL DEFAULT '[]',
        cost_usd      REAL NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_bench_results ON benchmark_results(benchmark_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_media_hash ON media_requests(payload_hash)",
)

_LOCK = threading.RLock()
_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def _hermes_home() -> Path:
    """Resolve the Hermes home directory, honouring ``HERMES_HOME``."""
    raw = os.environ.get("HERMES_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hermes"


def _data_dir() -> Path:
    """Return the directory the NEXUS database lives in.

    Hermes' helper resolves the *active profile* on every call, so the path is
    recomputed each time rather than cached — switching profiles moves the
    database with the profile instead of leaving stale data behind.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_NAME)
    except Exception:
        pass
    data_dir = _hermes_home() / "plugin-data" / PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def open_connection() -> sqlite3.Connection:
    """Open (and memoise) the NEXUS database connection.

    Uses Hermes' own storage helper when available so the file lands in the
    profile-scoped plugin-data directory and picks up Hermes' WAL handling;
    otherwise it falls back to the same layout for standalone use.

    Hermes' ``plugin_db`` opens a fresh connection per call and installs no
    row factory, so the connection is memoised here and given a row factory
    explicitly — the DAO reads rows by column name.
    """
    with _LOCK:
        key = str(_data_dir().resolve())
        conn = _CONNECTIONS.get(key)
        if conn is None:
            # Close connections left behind by a previous profile.
            for stale_key, stale in list(_CONNECTIONS.items()):
                try:
                    stale.close()
                except Exception:
                    pass
                _CONNECTIONS.pop(stale_key, None)

            try:
                from plugins.plugin_storage import plugin_db

                conn = plugin_db(PLUGIN_NAME)
            except Exception:
                conn = sqlite3.connect(
                    _data_dir() / "data.db", check_same_thread=False, timeout=5.0
                )
                conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            _CONNECTIONS[key] = conn
        _init_schema(conn)
        return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema if it is not there yet."""
    with _LOCK:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.commit()


def close_connection() -> None:
    """Close every memoed connection (used by tests)."""
    with _LOCK:
        for conn in _CONNECTIONS.values():
            try:
                conn.close()
            except Exception:
                pass
        _CONNECTIONS.clear()


# --------------------------------------------------------------------------- #
# Runs and steps
# --------------------------------------------------------------------------- #


def record_run(run_id: str, goal: str, task_type: str, status: str = "running") -> None:
    """Insert a run row."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, goal, task_type, status, created_at) VALUES (?,?,?,?,?)",
            (run_id, goal, task_type, status, time.time()),
        )
        conn.commit()


def finish_run(run_id: str, status: str, total_cost_usd: float, total_latency_ms: int,
               models_used: Sequence[str], report: dict[str, Any] | None = None) -> None:
    """Update a run row with its final accounting."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, total_cost_usd=?, total_latency_ms=?, "
            "models_used=?, report=? WHERE id=?",
            (
                status,
                time.time(),
                float(total_cost_usd),
                int(total_latency_ms),
                json.dumps(list(models_used)),
                json.dumps(report) if report is not None else None,
                run_id,
            ),
        )
        conn.commit()


def record_step(run_id: str, step: dict[str, Any]) -> None:
    """Insert one step row."""
    conn = open_connection()
    usage = step.get("usage") or {}
    with _LOCK:
        conn.execute(
            "INSERT INTO steps (run_id, seq, description, task_type, status, model_id, output, "
            "media_urls, latency_ms, attempts, prompt_tokens, completion_tokens, cost_usd, error, validation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                int(step.get("step_index", 0)),
                str(step.get("description", "")),
                str(step.get("task_type", "")),
                str(step.get("status", "")),
                step.get("model_id"),
                _truncate(str(step.get("output") or "")),
                json.dumps(list(step.get("media_urls") or [])),
                int(step.get("latency_ms", 0)),
                int(step.get("attempts", 0)),
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
                float(usage.get("cost_usd", 0.0)),
                json.dumps(step.get("error")) if step.get("error") else None,
                json.dumps(step.get("validation") or {}),
            ),
        )
        conn.commit()


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent runs, newest first.

    The row id breaks ties between runs started within the same clock tick.
    """
    conn = open_connection()
    with _LOCK:
        rows = conn.execute(
            "SELECT id, goal, task_type, status, created_at, finished_at, total_cost_usd, "
            "total_latency_ms, models_used FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["models_used"] = json.loads(item.get("models_used") or "[]")
        except (ValueError, TypeError):
            item["models_used"] = []
        results.append(item)
    return results


def run_detail(run_id: str) -> dict[str, Any] | None:
    """Return one run with its steps."""
    conn = open_connection()
    with _LOCK:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        steps = conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
    detail = dict(row)
    try:
        detail["models_used"] = json.loads(detail.get("models_used") or "[]")
    except (ValueError, TypeError):
        detail["models_used"] = []
    step_list = []
    for step in steps:
        item = dict(step)
        try:
            item["media_urls"] = json.loads(item.get("media_urls") or "[]")
        except (ValueError, TypeError):
            item["media_urls"] = []
        try:
            item["validation"] = json.loads(item.get("validation") or "{}")
        except (ValueError, TypeError):
            item["validation"] = {}
        step_list.append(item)
    detail["steps"] = step_list
    return detail


# --------------------------------------------------------------------------- #
# Routing statistics (the "learning" half of adaptive intelligence)
# --------------------------------------------------------------------------- #


def update_model_stats(model_id: str, task_type: str, *, success: bool, latency_ms: int,
                       cost_usd: float, alpha: float = 0.3) -> None:
    """Fold one verified outcome into the running statistics for a model.

    Latency and cost are tracked as exponentially weighted moving averages so a
    single slow request cannot dominate the history, while the success rate is
    kept as an exact count.
    """
    conn = open_connection()
    with _LOCK:
        row = conn.execute(
            "SELECT * FROM model_stats WHERE model_id=? AND task_type=?", (model_id, task_type)
        ).fetchone()
        now = time.time()
        if row is None:
            ewma_latency = float(latency_ms)
            ewma_cost = float(cost_usd)
            runs, successes, failures = 1, int(success), int(not success)
            total_cost = float(cost_usd)
        else:
            ewma_latency = (1 - alpha) * float(row["ewma_latency_ms"]) + alpha * float(latency_ms)
            ewma_cost = (1 - alpha) * float(row["ewma_cost_usd"]) + alpha * float(cost_usd)
            runs = int(row["runs"]) + 1
            successes = int(row["successes"]) + int(success)
            failures = int(row["failures"]) + int(not success)
            total_cost = float(row["total_cost_usd"]) + float(cost_usd)
        conn.execute(
            "INSERT OR REPLACE INTO model_stats (model_id, task_type, runs, successes, failures, "
            "ewma_latency_ms, ewma_cost_usd, total_cost_usd, last_used_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (model_id, task_type, runs, successes, failures, ewma_latency, ewma_cost, total_cost, now),
        )
        conn.commit()


def model_stats(task_type: str | None = None) -> list[dict[str, Any]]:
    """Return the routing statistics, optionally filtered by task type."""
    conn = open_connection()
    with _LOCK:
        if task_type:
            rows = conn.execute(
                "SELECT * FROM model_stats WHERE task_type=? ORDER BY successes DESC, runs ASC",
                (task_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM model_stats ORDER BY successes DESC, runs ASC"
            ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #


def create_benchmark(benchmark_id: str, suite: str, models: Sequence[str]) -> None:
    """Insert a benchmark run row."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "INSERT OR REPLACE INTO benchmarks (id, suite, created_at, models, status) VALUES (?,?,?,?,?)",
            (benchmark_id, suite, time.time(), json.dumps(list(models)), "running"),
        )
        conn.commit()


def record_benchmark_result(benchmark_id: str, result: dict[str, Any]) -> None:
    """Insert one benchmark result row."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "INSERT INTO benchmark_results (benchmark_id, model_id, task_index, task, latency_ms, "
            "prompt_tokens, completion_tokens, cost_usd, score, passed, output, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                benchmark_id,
                str(result.get("model_id", "")),
                int(result.get("task_index", 0)),
                str(result.get("task", "")),
                int(result.get("latency_ms", 0)),
                int(result.get("prompt_tokens", 0)),
                int(result.get("completion_tokens", 0)),
                float(result.get("cost_usd", 0.0)),
                result.get("score"),
                None if result.get("passed") is None else int(bool(result["passed"])),
                _truncate(str(result.get("output") or "")),
                json.dumps(result.get("error")) if result.get("error") else None,
                time.time(),
            ),
        )
        conn.commit()


def finish_benchmark(benchmark_id: str, status: str, summary: dict[str, Any]) -> None:
    """Update a benchmark row with its summary."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "UPDATE benchmarks SET status=?, summary=? WHERE id=?",
            (status, json.dumps(summary), benchmark_id),
        )
        conn.commit()


def list_benchmarks(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent benchmark runs, newest first.

    The row id breaks ties between benchmarks started within the same clock
    tick.
    """
    conn = open_connection()
    with _LOCK:
        rows = conn.execute(
            "SELECT * FROM benchmarks ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["models"] = json.loads(item.get("models") or "[]")
        except (ValueError, TypeError):
            item["models"] = []
        try:
            item["summary"] = json.loads(item.get("summary") or "null")
        except (ValueError, TypeError):
            item["summary"] = None
        results.append(item)
    return results


def benchmark_results(benchmark_id: str) -> list[dict[str, Any]]:
    """Return the per-model results of one benchmark."""
    conn = open_connection()
    with _LOCK:
        rows = conn.execute(
            "SELECT * FROM benchmark_results WHERE benchmark_id=? ORDER BY model_id, task_index",
            (benchmark_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Capability probes
# --------------------------------------------------------------------------- #


def save_capability_probe(model_id: str, capability: str, verified: bool, evidence: str) -> None:
    """Record the outcome of a capability probe."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "INSERT OR REPLACE INTO capability_probes (model_id, capability, verified, evidence, probed_at) "
            "VALUES (?,?,?,?,?)",
            (model_id, capability, int(verified), evidence[:500], time.time()),
        )
        conn.commit()


def capability_probes(model_id: str | None = None) -> list[dict[str, Any]]:
    """Return recorded capability probes."""
    conn = open_connection()
    with _LOCK:
        if model_id:
            rows = conn.execute(
                "SELECT * FROM capability_probes WHERE model_id=?", (model_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM capability_probes").fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Usage and spend
# --------------------------------------------------------------------------- #


def record_usage(kind: str, model_id: str | None, cost_usd: float, prompt_tokens: int = 0,
                 completion_tokens: int = 0, requests: int = 1, detail: str | None = None) -> None:
    """Record a spend event."""
    conn = open_connection()
    with _LOCK:
        conn.execute(
            "INSERT INTO usage_events (ts, kind, model_id, cost_usd, prompt_tokens, "
            "completion_tokens, requests, detail) VALUES (?,?,?,?,?,?,?,?)",
            (
                time.time(),
                kind,
                model_id,
                float(cost_usd),
                int(prompt_tokens),
                int(completion_tokens),
                int(requests),
                detail,
            ),
        )
        conn.commit()


def usage_series(days: int = 30) -> list[dict[str, Any]]:
    """Return the tracked spend grouped by day, oldest first.

    A chart needs a series, and a summary alone cannot draw one. The bucket
    is the UTC day the event was recorded on, so the series is stable
    regardless of when it is read.
    """
    cutoff = time.time() - max(1, int(days)) * 86400
    with _LOCK:
        rows = open_connection().execute(
            "SELECT CAST(ts / 86400 AS INTEGER) AS day, "
            "COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(prompt_tokens + completion_tokens),0) AS tokens, "
            "COALESCE(SUM(requests),0) AS requests "
            "FROM usage_events WHERE ts >= ? GROUP BY day ORDER BY day",
            (cutoff,),
        ).fetchall()
    series = []
    for row in rows:
        # 86400-second buckets start at the epoch, so the day number is a
        # date offset rather than a date: convert it back.
        day = int(row["day"]) * 86400
        series.append(
            {
                "day": time.strftime("%Y-%m-%d", time.gmtime(day)),
                "ts": float(day),
                "cost_usd": round(float(row["cost"]), 8),
                "tokens": int(row["tokens"]),
                "requests": int(row["requests"]),
            }
        )
    return series


def spend_since(since: float) -> float:
    """Return the estimated USD spent since ``since`` (a unix timestamp)."""
    conn = open_connection()
    with _LOCK:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM usage_events WHERE ts >= ?",
            (float(since),),
        ).fetchone()
    return float(row["total"] if row else 0.0)


def usage_summary(days: int = 30) -> dict[str, Any]:
    """Summarise tracked usage over the last ``days`` days."""
    conn = open_connection()
    cutoff = time.time() - max(1, int(days)) * 86400
    with _LOCK:
        totals = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS cost, COALESCE(SUM(prompt_tokens),0) AS pt, "
            "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(requests),0) AS rq, COUNT(*) AS events "
            "FROM usage_events WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        by_model = conn.execute(
            "SELECT model_id, COUNT(*) AS events, COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(prompt_tokens + completion_tokens),0) AS tokens "
            "FROM usage_events WHERE ts >= ? AND model_id IS NOT NULL "
            "GROUP BY model_id ORDER BY cost DESC",
            (cutoff,),
        ).fetchall()
        by_kind = conn.execute(
            "SELECT kind, COUNT(*) AS events, COALESCE(SUM(cost_usd),0) AS cost "
            "FROM usage_events WHERE ts >= ? GROUP BY kind ORDER BY cost DESC",
            (cutoff,),
        ).fetchall()
    return {
        "window_days": int(days),
        "total_cost_usd": round(float(totals["cost"] if totals else 0.0), 8),
        "prompt_tokens": int(totals["pt"] if totals else 0),
        "completion_tokens": int(totals["ct"] if totals else 0),
        "requests": int(totals["rq"] if totals else 0),
        "events": int(totals["events"] if totals else 0),
        "by_model": [dict(row) for row in by_model],
        "by_kind": [dict(row) for row in by_kind],
    }


# --------------------------------------------------------------------------- #
# Media requests (duplicate suppression for expensive generations)
# --------------------------------------------------------------------------- #


def record_media_request(request_id: str, model_id: str, payload_hash: str) -> None:
    """Record that a generation request was submitted."""
    conn = open_connection()
    now = time.time()
    with _LOCK:
        conn.execute(
            "INSERT OR REPLACE INTO media_requests (request_id, model_id, payload_hash, status, "
            "media_urls, cost_usd, created_at, updated_at) VALUES (?,?,?,'submitted','[]',0,?,?)",
            (request_id, model_id, payload_hash, now, now),
        )
        conn.commit()


def update_media_request(
    request_id: str,
    status: str,
    media_urls: Sequence[str] | None = None,
    cost_usd: float | None = None,
) -> None:
    """Record the outcome of a generation request."""
    conn = open_connection()
    with _LOCK:
        if media_urls is not None:
            conn.execute(
                "UPDATE media_requests SET status=?, media_urls=?, cost_usd=COALESCE(?, cost_usd), "
                "updated_at=? WHERE request_id=?",
                (status, json.dumps(list(media_urls)), cost_usd, time.time(), request_id),
            )
        elif cost_usd is not None:
            conn.execute(
                "UPDATE media_requests SET status=?, cost_usd=?, updated_at=? WHERE request_id=?",
                (status, cost_usd, time.time(), request_id),
            )
        else:
            conn.execute(
                "UPDATE media_requests SET status=?, updated_at=? WHERE request_id=?",
                (status, time.time(), request_id),
            )
        conn.commit()


def list_media_requests(limit: int = 50) -> list[dict[str, Any]]:
    """Return the recorded generation requests, newest first.

    Only requests that were actually submitted are recorded, so this is a
    history of real generations — never a plan of what might be generated.

    The row id breaks ties. Two requests made within the same clock tick
    share a timestamp, and the order between them must still be the order
    they were submitted in.
    """
    conn = open_connection()
    with _LOCK:
        rows = conn.execute(
            "SELECT * FROM media_requests ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["media_urls"] = json.loads(item.get("media_urls") or "[]")
        except (ValueError, TypeError):
            item["media_urls"] = []
        results.append(item)
    return results


def find_media_request(payload_hash: str) -> str | None:
    """Return the most recent request id submitted for a payload hash."""
    conn = open_connection()
    with _LOCK:
        row = conn.execute(
            "SELECT request_id FROM media_requests WHERE payload_hash=? ORDER BY created_at DESC LIMIT 1",
            (payload_hash,),
        ).fetchone()
    return str(row["request_id"]) if row else None


def media_request(request_id: str) -> dict[str, Any] | None:
    """Return one recorded generation request."""
    conn = open_connection()
    with _LOCK:
        row = conn.execute(
            "SELECT * FROM media_requests WHERE request_id=?", (request_id,)
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["media_urls"] = json.loads(item.get("media_urls") or "[]")
    except (ValueError, TypeError):
        item["media_urls"] = []
    return item


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #


def prune(retention_days: int) -> int:
    """Delete history older than ``retention_days``. Returns rows removed."""
    cutoff = time.time() - max(1, int(retention_days)) * 86400
    conn = open_connection()
    removed = 0
    with _LOCK:
        # Each table keeps its timestamp under a different name.
        for table, column in (
            ("benchmarks", "created_at"),
            ("benchmark_results", "created_at"),
            ("usage_events", "ts"),
        ):
            cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
            removed += cursor.rowcount or 0
        cursor = conn.execute("DELETE FROM steps WHERE run_id IN (SELECT id FROM runs WHERE created_at < ?)",
                              (cutoff,))
        removed += cursor.rowcount or 0
        cursor = conn.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,))
        removed += cursor.rowcount or 0
        conn.commit()
    return removed


def _truncate(text: str, limit: int = 4000) -> str:
    """Bound the stored output excerpt."""
    if len(text) <= limit:
        return text
    return text[:limit] + "… [truncated]"


__all__ = [
    "PLUGIN_NAME",
    "benchmark_results",
    "capability_probes",
    "close_connection",
    "create_benchmark",
    "finish_benchmark",
    "finish_run",
    "find_media_request",
    "list_benchmarks",
    "list_media_requests",
    "media_request",
    "model_stats",
    "open_connection",
    "prune",
    "recent_runs",
    "record_benchmark_result",
    "record_media_request",
    "record_run",
    "record_step",
    "record_usage",
    "run_detail",
    "save_capability_probe",
    "spend_since",
    "update_media_request",
    "update_model_stats",
    "usage_series",
    "usage_summary",
]
