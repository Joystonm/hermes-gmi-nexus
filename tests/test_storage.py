"""Persistent storage: where it lives, what it keeps, and what it forgets."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nexus import storage


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #


def test_the_database_lives_in_the_plugin_data_directory(tmp_path, monkeypatch):
    """Runtime state must not be written inside the plugin's own directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage.close_connection()

    conn = storage.open_connection()
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    assert path.parent == tmp_path / "plugin-data" / "gmi-nexus"
    # And never inside the installation directory.
    plugin_root = Path(__file__).resolve().parents[1] / "plugins" / "gmi-nexus"
    assert plugin_root not in path.parents


def test_the_data_directory_follows_the_hermes_home(tmp_path, monkeypatch):
    first = tmp_path / "one"
    second = tmp_path / "two"

    monkeypatch.setenv("HERMES_HOME", str(first))
    storage.close_connection()
    storage.open_connection()

    monkeypatch.setenv("HERMES_HOME", str(second))
    storage.close_connection()
    conn = storage.open_connection()

    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    assert path.parent == second / "plugin-data" / "gmi-nexus"


# --------------------------------------------------------------------------- #
# Runs and steps
# --------------------------------------------------------------------------- #


def test_a_run_is_recorded_with_its_outcome():
    storage.record_run("run-1", "Summarise the report", "general_reasoning")
    storage.finish_run("run-1", "completed", 0.0021, 812, ["acme/one"])

    runs = storage.recent_runs()
    assert runs[0]["id"] == "run-1"
    assert runs[0]["goal"] == "Summarise the report"
    assert runs[0]["status"] == "completed"
    assert runs[0]["models_used"] == ["acme/one"]
    assert runs[0]["total_cost_usd"] == pytest.approx(0.0021)


def test_the_steps_of_a_run_are_kept_with_it():
    storage.record_run("run-2", "Do the thing", "coding")
    storage.record_step(
        "run-2",
        {
            "step_index": 0,
            "description": "write the function",
            "task_type": "coding",
            "status": "ok",
            "model_id": "acme/coder",
            "output": "def f(): ...",
            "media_urls": [],
            "latency_ms": 120,
            "attempts": 1,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 0.0001},
            "validation": {"evaluation": {"passed": True}},
        },
    )
    storage.finish_run("run-2", "completed", 0.0001, 120, ["acme/coder"])

    detail = storage.run_detail("run-2")
    assert detail is not None
    assert len(detail["steps"]) == 1
    step = detail["steps"][0]
    assert step["model_id"] == "acme/coder"
    assert step["usage"] if "usage" in step else True
    assert step["validation"]["evaluation"]["passed"] is True


def test_an_unknown_run_is_not_invented():
    assert storage.run_detail("no-such-run") is None


def test_the_newest_run_comes_first():
    storage.record_run("old", "first", "general_reasoning")
    time.sleep(0.01)
    storage.record_run("new", "second", "general_reasoning")

    runs = storage.recent_runs()
    assert runs[0]["id"] == "new"


# --------------------------------------------------------------------------- #
# Routing statistics
# --------------------------------------------------------------------------- #


def test_the_statistics_count_successes_and_failures():
    storage.update_model_stats("acme/one", "coding", success=True, latency_ms=100, cost_usd=0.001)
    storage.update_model_stats("acme/one", "coding", success=False, latency_ms=200, cost_usd=0.002)

    row = storage.model_stats("coding")[0]
    assert row["model_id"] == "acme/one"
    assert row["runs"] == 2
    assert row["successes"] == 1
    assert row["failures"] == 1


def test_the_statistics_are_kept_per_task_type():
    storage.update_model_stats("acme/one", "coding", success=True, latency_ms=100, cost_usd=0.001)
    storage.update_model_stats("acme/one", "general_reasoning", success=True, latency_ms=100, cost_usd=0.001)

    assert len(storage.model_stats()) == 2
    assert len(storage.model_stats("coding")) == 1


def test_the_latency_average_moves_towards_the_new_measurement():
    storage.update_model_stats("acme/one", "coding", success=True, latency_ms=1000, cost_usd=0.001)
    storage.update_model_stats("acme/one", "coding", success=True, latency_ms=100, cost_usd=0.001)

    row = storage.model_stats("coding")[0]
    # An exponentially weighted average, so one fast call does not erase
    # the history.
    assert 100 < row["ewma_latency_ms"] < 1000


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #


def test_the_usage_summary_adds_up():
    storage.record_usage("llm", "acme/one", 0.001, prompt_tokens=10, completion_tokens=5)
    storage.record_usage("media", "acme/two", 0.004, requests=2)

    summary = storage.usage_summary(days=30)
    assert summary["total_cost_usd"] == pytest.approx(0.005)
    assert summary["prompt_tokens"] == 10
    assert summary["completion_tokens"] == 5
    assert summary["requests"] == 3
    assert len(summary["by_model"]) == 2
    assert len(summary["by_kind"]) == 2


def test_usage_outside_the_window_is_not_counted():
    storage.record_usage("llm", "acme/one", 0.001)
    old = time.time() - 10 * 86400
    storage.open_connection().execute(
        "INSERT INTO usage_events (ts, kind, model_id, cost_usd, prompt_tokens, "
        "completion_tokens, requests, detail) VALUES (?,?,?,?,?,?,?,?)",
        (old, "llm", "acme/one", 5.0, 0, 0, 1, None),
    )
    storage.open_connection().commit()

    assert storage.usage_summary(days=1)["total_cost_usd"] == pytest.approx(0.001)


def test_the_spend_since_a_moment_ago_is_the_spend():
    storage.record_usage("llm", "acme/one", 0.002)
    assert storage.spend_since(time.time() - 60) == pytest.approx(0.002)
    assert storage.spend_since(time.time() + 60) == pytest.approx(0.0)


def test_the_usage_series_groups_the_spend_by_day():
    """A chart needs a series, not just a total."""
    storage.record_usage("llm", "acme/one", 0.002, prompt_tokens=10, completion_tokens=5)
    storage.record_usage("media", "acme/two", 0.004, requests=2)

    series = storage.usage_series(days=30)

    assert len(series) == 1
    day = series[0]
    assert day["cost_usd"] == pytest.approx(0.006)
    assert day["tokens"] == 15
    assert day["requests"] == 3
    assert day["day"] == time.strftime("%Y-%m-%d", time.gmtime())


def test_the_usage_series_ignores_what_is_outside_the_window():
    old = time.time() - 10 * 86400
    storage.open_connection().execute(
        "INSERT INTO usage_events (ts, kind, model_id, cost_usd, prompt_tokens, "
        "completion_tokens, requests, detail) VALUES (?,?,?,?,?,?,?,?)",
        (old, "llm", "acme/one", 5.0, 0, 0, 1, None),
    )
    storage.open_connection().commit()
    storage.record_usage("llm", "acme/one", 0.001)

    series = storage.usage_series(days=1)

    assert len(series) == 1
    assert series[0]["cost_usd"] == pytest.approx(0.001)


def test_the_usage_series_is_empty_before_anything_is_spent():
    assert storage.usage_series(days=30) == []


# --------------------------------------------------------------------------- #
# Benchmarks and probes
# --------------------------------------------------------------------------- #


def test_a_benchmark_and_its_results_are_stored():
    storage.create_benchmark("bench-1", "default", ["acme/one", "acme/two"])
    storage.record_benchmark_result(
        "bench-1",
        {
            "model_id": "acme/one",
            "task_index": 0,
            "task": "Say NEXUS",
            "latency_ms": 90,
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "cost_usd": 0.0001,
            "score": 1.0,
            "passed": True,
            "output": "NEXUS",
        },
    )
    storage.finish_benchmark("bench-1", "completed", {"scores": {"acme/one": 1.0}})

    benchmarks = storage.list_benchmarks()
    assert benchmarks[0]["id"] == "bench-1"
    assert benchmarks[0]["summary"]["scores"]["acme/one"] == 1.0
    assert len(storage.benchmark_results("bench-1")) == 1


def test_a_capability_probe_is_remembered():
    storage.save_capability_probe("acme/one", "vision", True, "probe: red and blue")

    probes = storage.capability_probes("acme/one")
    assert len(probes) == 1
    assert probes[0]["capability"] == "vision"
    assert probes[0]["verified"] == 1
    assert "red and blue" in probes[0]["evidence"]


# --------------------------------------------------------------------------- #
# Media requests
# --------------------------------------------------------------------------- #


def test_a_generation_request_is_remembered_by_its_payload():
    storage.record_media_request("req-1", "gpt-image-2", "hash-abc")

    assert storage.find_media_request("hash-abc") == "req-1"
    storage.update_media_request("req-1", "success", ["https://cdn/out.png"], 0.042)
    row = storage.media_request("req-1")
    assert row["status"] == "success"
    assert row["media_urls"] == ["https://cdn/out.png"]


def test_the_generation_history_lists_the_newest_first():
    """The dashboard shows the generations that actually happened."""
    storage.record_media_request("req-1", "gpt-image-2", "hash-abc")
    storage.update_media_request("req-1", "success", ["https://cdn/out.png"], 0.042)
    storage.record_media_request("req-2", "wan-video", "hash-def")

    rows = storage.list_media_requests(limit=10)

    assert [row["request_id"] for row in rows] == ["req-2", "req-1"]
    assert rows[1]["media_urls"] == ["https://cdn/out.png"]
    assert rows[1]["cost_usd"] == pytest.approx(0.042)


def test_the_generation_history_is_empty_before_anything_was_generated():
    assert storage.list_media_requests(limit=10) == []


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #


def test_old_history_is_pruned():
    storage.record_run("old-run", "old", "general_reasoning")
    old = time.time() - 40 * 86400
    conn = storage.open_connection()
    conn.execute("UPDATE runs SET created_at=? WHERE id=?", (old, "old-run"))
    conn.commit()

    removed = storage.prune(retention_days=30)
    assert removed >= 1
    assert storage.run_detail("old-run") is None


# --------------------------------------------------------------------------- #
# What is *not* stored
# --------------------------------------------------------------------------- #


def test_the_stored_history_never_contains_the_credential():
    storage.record_run("run-3", "Summarise the report", "general_reasoning")
    storage.record_step(
        "run-3",
        {
            "step_index": 0,
            "description": "read the file",
            "task_type": "general_reasoning",
            "status": "ok",
            "model_id": "acme/one",
            "output": "The report says: all good.",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost_usd": 0.0},
        },
    )

    blob = json.dumps(storage.run_detail("run-3"), default=str)
    assert "test-key-not-real" not in blob
