from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from calendar import monthrange

from sqlalchemy import and_

from app.models import AttendanceSession, RegularSchedule, SessionOverride, db
from app.utils.audit_utils import log_audit

RENDERED_STATUSES = {"Present", "Make-up", "Rescheduled", "Billed", "Non-billable"}
MISSED_STATUSES = {"Absent", "Rescheduled", "Cancelled"}


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
                end_time=schedule.end_time or _time_add(schedule.start_time, schedule.duration_hours),
                duration_hours=schedule.duration_hours,
                session_type="regular",
                source_type="generated",
                linked_regular_schedule_id=schedule.id,
                status="",
            )
            db.session.add(session)
            created += 1
    db.session.commit()
    if created:
        log_audit("attendance_generate_month", "AttendanceSession", None, f"{year}-{month:02d}: {created}")
    return created


def _schedule_applies_on_date(schedule: RegularSchedule, session_date: date) -> bool:
    if not schedule.active:
        return False
    if schedule.effective_from and session_date < schedule.effective_from:
        return False
    if schedule.effective_to and session_date > schedule.effective_to:
        return False
    return session_date.weekday() == schedule.day_of_week


def sync_future_generated_sessions_for_schedule_change(
    schedule: RegularSchedule,
    previous_day_of_week: int,
    previous_start_time: time,
    previous_therapist_id: int,
    effective_from: date,
) -> tuple[int, int]:
    """Forward-effective sync for generated attendance after schedule edits.

    - Only touches generated, blank sessions on/after effective_from.
    - Preserves past dates, recorded statuses, and manual override-related sessions.
    """
    generated_future = AttendanceSession.query.filter(
        AttendanceSession.student_id == schedule.student_id,
        AttendanceSession.source_type == "generated",
        AttendanceSession.session_date >= effective_from,
    ).all()
    if not generated_future:
        return 0, 0

    override_pairs = db.session.query(SessionOverride.original_session_id, SessionOverride.new_session_id).all()
    protected_ids = {oid for oid, _ in override_pairs if oid} | {nid for _, nid in override_pairs if nid}

    horizon_end = max(s.session_date for s in generated_future)
    removed = 0
    added = 0

    # Remove stale generated sessions from old permanent schedule only when safe.
    for sess in generated_future:
        if sess.id in protected_ids or sess.status:
            continue
        if not (
            sess.linked_regular_schedule_id == schedule.id
            or (
                sess.session_date.weekday() == previous_day_of_week
                and sess.start_time == previous_start_time
                and sess.therapist_id == previous_therapist_id
            )
        ):
            continue
        if _schedule_applies_on_date(schedule, sess.session_date) and sess.start_time == schedule.start_time and sess.therapist_id == schedule.therapist_id:
            continue
        db.session.delete(sess)
        removed += 1

    # Add missing future generated sessions matching the new permanent schedule.
    cursor = effective_from
    while cursor <= horizon_end:
        if _schedule_applies_on_date(schedule, cursor):
            has_existing_slot = AttendanceSession.query.filter(
                AttendanceSession.student_id == schedule.student_id,
                AttendanceSession.session_date == cursor,
                AttendanceSession.start_time == schedule.start_time,
            ).first()
            if not has_existing_slot:
                db.session.add(
                    AttendanceSession(
                        student_id=schedule.student_id,
                        therapist_id=schedule.therapist_id,
                        session_date=cursor,
                        start_time=schedule.start_time,
                        end_time=schedule.end_time or _time_add(schedule.start_time, schedule.duration_hours),
                        duration_hours=schedule.duration_hours,
                        session_type="regular",
                        source_type="generated",
                        linked_regular_schedule_id=schedule.id,
                        status="",
                    )
                )
                added += 1
        cursor += timedelta(days=1)

    return removed, added


def preview_future_generated_sessions_sync(
    schedule: RegularSchedule,
    proposed_day_of_week: int,
    proposed_start_time: time,
    proposed_therapist_id: int,
    proposed_duration_hours: float,
    proposed_end_time: time | None,
    proposed_active: bool,
    previous_day_of_week: int,
    previous_start_time: time,
    previous_therapist_id: int,
    effective_from: date,
) -> dict[str, int]:
    """Read-only preview counts for forward-effective schedule sync."""
    generated_future = AttendanceSession.query.filter(
        AttendanceSession.student_id == schedule.student_id,
        AttendanceSession.source_type == "generated",
        AttendanceSession.session_date >= effective_from,
    ).all()
    if not generated_future:
        return {"to_remove": 0, "to_add": 0, "recorded_preserved": 0, "override_preserved": 0}

    override_pairs = db.session.query(SessionOverride.original_session_id, SessionOverride.new_session_id).all()
    protected_ids = {oid for oid, _ in override_pairs if oid} | {nid for _, nid in override_pairs if nid}
    recorded_preserved = sum(1 for s in generated_future if s.status)
    override_preserved = sum(1 for s in generated_future if s.id in protected_ids)

    def _proposed_applies(session_date: date) -> bool:
        if not proposed_active:
            return False
        if schedule.effective_from and session_date < schedule.effective_from:
            return False
        if schedule.effective_to and session_date > schedule.effective_to:
            return False
        return session_date.weekday() == proposed_day_of_week

    to_remove = 0
    for sess in generated_future:
        if sess.id in protected_ids or sess.status:
            continue
        if not (
            sess.linked_regular_schedule_id == schedule.id
            or (
                sess.session_date.weekday() == previous_day_of_week
                and sess.start_time == previous_start_time
                and sess.therapist_id == previous_therapist_id
            )
        ):
            continue
        if _proposed_applies(sess.session_date) and sess.start_time == proposed_start_time and sess.therapist_id == proposed_therapist_id:
            continue
        to_remove += 1

    horizon_end = max(s.session_date for s in generated_future)
    to_add = 0
    cursor = effective_from
    while cursor <= horizon_end:
        if _proposed_applies(cursor):
            has_existing_slot = AttendanceSession.query.filter(
                AttendanceSession.student_id == schedule.student_id,
                AttendanceSession.session_date == cursor,
                AttendanceSession.start_time == proposed_start_time,
            ).first()
            if not has_existing_slot:
                to_add += 1
        cursor += timedelta(days=1)

    return {
        "to_remove": to_remove,
        "to_add": to_add,
        "recorded_preserved": recorded_preserved,
        "override_preserved": override_preserved,
    }


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
    db.session.commit()
    log_audit("makeup_session_created", "AttendanceSession", session.id, notes)
    return session


def update_session_status(session_id: int, status: str, notes: str = "") -> None:
    session = AttendanceSession.query.get_or_404(session_id)
    old = session.status
    session.status = status
    if notes:
        session.notes = notes
    db.session.commit()
    log_audit("attendance_status_updated", "AttendanceSession", session_id, f"{old}->{status}")


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




def _effective_rendered_sessions(start_date: date, end_date: date):
    replaced_ids = {
        row[0]
        for row in db.session.query(SessionOverride.original_session_id)
        .filter(SessionOverride.original_session_id.isnot(None))
        .all()
    }

    sessions = AttendanceSession.query.filter(
        AttendanceSession.session_date >= start_date,
        AttendanceSession.session_date <= end_date,
        AttendanceSession.status.in_(list(RENDERED_STATUSES)),
    ).all()

    return [s for s in sessions if s.id not in replaced_ids]


def weekly_student_hours(start_date: date, end_date: date) -> dict[int, float]:
    rows = _effective_rendered_sessions(start_date, end_date)
    result: dict[int, float] = defaultdict(float)
    for r in rows:
        result[r.student_id] += r.duration_hours
    return dict(result)


def weekly_therapist_hours(start_date: date, end_date: date) -> dict[int, float]:
    rows = _effective_rendered_sessions(start_date, end_date)
    result: dict[int, float] = defaultdict(float)
    for r in rows:
        result[r.therapist_id] += r.duration_hours
    return dict(result)


def missed_recovery_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    student_id: int | None = None,
    therapist_id: int | None = None,
) -> dict[str, float]:
    missed_query = AttendanceSession.query.filter(AttendanceSession.status.in_(list(MISSED_STATUSES)))
    if start_date:
        missed_query = missed_query.filter(AttendanceSession.session_date >= start_date)
    if end_date:
        missed_query = missed_query.filter(AttendanceSession.session_date <= end_date)
    if student_id:
        missed_query = missed_query.filter(AttendanceSession.student_id == student_id)
    if therapist_id:
        missed_query = missed_query.filter(AttendanceSession.therapist_id == therapist_id)

    missed_sessions = missed_query.all()
    missed_ids = [s.id for s in missed_sessions]
    missed_hours = round(sum(s.duration_hours for s in missed_sessions), 2)

    recovered_hours = 0.0
    recovered_count = 0
    if missed_ids:
        replacement_ids = [
            row[0]
            for row in db.session.query(SessionOverride.new_session_id)
            .filter(SessionOverride.original_session_id.in_(missed_ids))
            .all()
        ]
        if replacement_ids:
            recovered_query = AttendanceSession.query.filter(
                AttendanceSession.id.in_(replacement_ids),
                AttendanceSession.session_type == "makeup",
                AttendanceSession.status.in_(list(RENDERED_STATUSES)),
            )
            if student_id:
                recovered_query = recovered_query.filter(AttendanceSession.student_id == student_id)
            if therapist_id:
                recovered_query = recovered_query.filter(AttendanceSession.therapist_id == therapist_id)
            recovered_sessions = recovered_query.all()
            recovered_hours = round(sum(s.duration_hours for s in recovered_sessions), 2)
            recovered_count = len(recovered_sessions)

    return {
        "missed_sessions": len(missed_sessions),
        "missed_hours": missed_hours,
        "recovered_makeup_sessions": recovered_count,
        "recovered_makeup_hours": recovered_hours,
        "remaining_missed_hours": round(max(missed_hours - recovered_hours, 0), 2),
    }
