"""Persistence for reconciliation reports."""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gstr2b_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ledger_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    results_json: Mapped[str] = mapped_column(Text, nullable=False)


class ReconciliationException(Base):
    __tablename__ = "reconciliation_exceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), nullable=False, index=True)
    result_index: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./reconciliation.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


def init_db() -> None:
    Base.metadata.create_all(engine)


def save_run(report: dict, gstr2b_hash: str, ledger_hash: str) -> int:
    summary = {key: value for key, value in report.items() if key != "results"}
    with Session(engine) as session:
        results = [dict(result) for result in report.get("results", [])]
        run = ReconciliationRun(
            created_at=datetime.now(timezone.utc),
            gstr2b_hash=gstr2b_hash,
            ledger_hash=ledger_hash,
            summary_json=json.dumps(summary),
            results_json=json.dumps(results),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        for index, result in enumerate(results):
            if result.get("status") == "MATCHED":
                continue
            exception = ReconciliationException(run_id=run.id, result_index=index)
            session.add(exception)
            session.flush()
            result["exception_id"] = exception.id
            result["resolution_status"] = exception.resolution_status
            result["resolution_note"] = exception.resolution_note
        run.results_json = json.dumps(results)
        session.commit()
        return run.id


def list_runs() -> list[dict]:
    with Session(engine) as session:
        runs = session.scalars(select(ReconciliationRun).order_by(ReconciliationRun.created_at.desc())).all()
        return [_run_summary(run) for run in runs]


def get_run(run_id: int) -> dict | None:
    with Session(engine) as session:
        run = session.get(ReconciliationRun, run_id)
        if run is None:
            return None
        summary = json.loads(run.summary_json)
        return {
            "id": run.id,
            "created_at": run.created_at.isoformat(),
            "gstr2b_hash": run.gstr2b_hash,
            "ledger_hash": run.ledger_hash,
            **summary,
            "results": _with_resolution_fields(session, run),
        }


def update_exception(run_id: int, exception_id: int, status: str, note: str | None) -> dict | None:
    if status not in {"OPEN", "RESOLVED", "IGNORED"}:
        raise ValueError("status must be OPEN, RESOLVED, or IGNORED")
    with Session(engine) as session:
        exception = session.get(ReconciliationException, exception_id)
        if exception is None or exception.run_id != run_id:
            return None
        exception.resolution_status = status
        exception.resolution_note = note
        session.commit()
        run = session.get(ReconciliationRun, run_id)
        result = json.loads(run.results_json)[exception.result_index]
        result["resolution_status"] = status
        result["resolution_note"] = note
        stored_results = json.loads(run.results_json)
        stored_results[exception.result_index] = result
        run.results_json = json.dumps(stored_results)
        session.commit()
        return result


def _run_summary(run: ReconciliationRun) -> dict:
    return {
        "id": run.id,
        "created_at": run.created_at.isoformat(),
        "gstr2b_hash": run.gstr2b_hash,
        "ledger_hash": run.ledger_hash,
        **json.loads(run.summary_json),
    }


def _with_resolution_fields(session: Session, run: ReconciliationRun) -> list[dict]:
    results = json.loads(run.results_json)
    exceptions = session.scalars(
        select(ReconciliationException).where(ReconciliationException.run_id == run.id)
    ).all()
    for exception in exceptions:
        result = results[exception.result_index]
        result["exception_id"] = exception.id
        result["resolution_status"] = exception.resolution_status
        result["resolution_note"] = exception.resolution_note
    return results
