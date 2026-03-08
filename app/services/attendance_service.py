from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from calendar import monthrange

from sqlalchemy import and_

from app.models import AttendanceSession, AuditLog, RegularSchedule, SessionOverride, db

RENDERED_STATUSES = {"Present", "Make-up", "Rescheduled"}


def _time_add(start: time, duration_hours: float) -> time:
    dt = datetime.combine(date.today(), start) + timedelta(hours=duration_hours)
    return dt.time().replace(second=0, microsecond=0)


def generate_monthly_sessions(year: int, month: int) -> int:
    """Generate attendance sessions for all active regular schedules for a month.

    Status remains blank by default for upcoming schedules until staff updates attendance.
    """
    _, days = monthrange(year, month)
    created = 0
    schedules = RegularSchedule.query.filter_by(active=True).all()
    for schedule in schedules:
        for day in range(1, days + 1):
            session_date = date(year, month, day)
            if session_date.weekday() != schedule.day_of_week:
                continue
            if schedule.effective_from and session_date < schedule.effective_from:
                continue
            if schedule.effective_to and session_date > schedule.effective_to:
                continue
            exists = AttendanceSession.query.filter_by(
                student_id=schedule.student_id,
                session_date=session_date,
                start_time=schedule.start_time,
                source_type="generated",
            ).first()
            if exists:
                continue
            session = AttendanceSession(
                student_id=schedule.student_id,
                therapist_id=schedule.therapist_id,
                session_date=session_date,
                start_time=schedule.start_time,
                end_time=_time_add(schedule.start_time, schedule.duration_hours),
                duration_hours=schedule.duration_hours,
                session_type="regular",
                source_type="generated",
                linked_regular_schedule_id=schedule.id,
                status="",
            )
            db.session.add(session)
            created += 1
    if created:
        db.session.add(AuditLog(action="generate_month", entity_type="AttendanceSession", details=f"{year}-{month:02d}: {created}"))
    db.session.commit()
    return created


def create_makeup_session(
    student_id: int,
    therapist_id: int,
    session_date: date,
    start_time: time,
    duration_hours: float,
    override_type: str = "makeup",
    original_session_id: int | None = None,
    notes: str = "",
) -> AttendanceSession:
    session = AttendanceSession(
        student_id=student_id,
        therapist_id=therapist_id,
        session_date=session_date,
        start_time=start_time,
        end_time=_time_add(start_time, duration_hours),
        duration_hours=duration_hours,
        session_type="makeup",
        source_type="manual",
        status="",
        notes=notes,
    )
    db.session.add(session)
    db.session.flush()

    override = SessionOverride(
        original_session_id=original_session_id,
        new_session_id=session.id,
        override_type=override_type,
        reason=notes,
    )
    db.session.add(override)
    db.session.add(AuditLog(action="add_makeup", entity_type="AttendanceSession", entity_id=session.id, details=notes))
    db.session.commit()
    return session


def update_session_status(session_id: int, status: str, notes: str = "") -> None:
    session = AttendanceSession.query.get_or_404(session_id)
    old = session.status
    session.status = status
    if notes:
        session.notes = notes
    db.session.add(AuditLog(action="status_change", entity_type="AttendanceSession", entity_id=session_id, details=f"{old}->{status}"))
    db.session.commit()


def get_month_sessions(year: int, month: int, therapist_id: int | None = None, student_id: int | None = None):
    start = date(year, month, 1)
    _, days = monthrange(year, month)
    end = date(year, month, days)
    query = AttendanceSession.query.filter(and_(AttendanceSession.session_date >= start, AttendanceSession.session_date <= end))
    if therapist_id:
        query = query.filter(AttendanceSession.therapist_id == therapist_id)
    if student_id:
        query = query.filter(AttendanceSession.student_id == student_id)
    sessions = query.order_by(AttendanceSession.session_date, AttendanceSession.start_time).all()
    grouped = defaultdict(list)
    for s in sessions:
        grouped[s.session_date].append(s)
    return grouped


def weekly_student_hours(start_date: date, end_date: date) -> dict[int, float]:
    rows = AttendanceSession.query.filter(
        AttendanceSession.session_date >= start_date,
        AttendanceSession.session_date <= end_date,
        AttendanceSession.status.in_(list(RENDERED_STATUSES)),
    ).all()
    result: dict[int, float] = defaultdict(float)
    for r in rows:
        result[r.student_id] += r.duration_hours
    return dict(result)


def weekly_therapist_hours(start_date: date, end_date: date) -> dict[int, float]:
    rows = AttendanceSession.query.filter(
        AttendanceSession.session_date >= start_date,
        AttendanceSession.session_date <= end_date,
        AttendanceSession.status.in_(list(RENDERED_STATUSES)),
    ).all()
    result: dict[int, float] = defaultdict(float)
    for r in rows:
        result[r.therapist_id] += r.duration_hours
    return dict(result)
