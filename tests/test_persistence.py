import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import persistence


def test_exception_resolution_round_trip(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(persistence, "engine", persistence.create_engine(f"sqlite:///{db_path}"))
    persistence.init_db()
    run_id = persistence.save_run(
        {"results": [{"status": "MISSING_IN_LEDGER", "reason": "test"}]}, "a" * 64, "b" * 64
    )
    stored = persistence.get_run(run_id)
    exception_id = stored["results"][0]["exception_id"]
    updated = persistence.update_exception(run_id, exception_id, "RESOLVED", "Booked next period")
    assert updated["resolution_status"] == "RESOLVED"
    assert persistence.get_run(run_id)["results"][0]["resolution_note"] == "Booked next period"
