from __future__ import annotations

from datetime import date, time

import pytest
from openpyxl import Workbook

from app.models import AttendanceSession, BillingAdvice, RegularSchedule, Student, Therapist, db
from app.services.attendance_service import create_makeup_session, generate_monthly_sessions, weekly_student_hours, weekly_therapist_hours
from app.services.billing_service import WEEKDAY_RATE, generate_billing_advices_for_cycle, generate_billing_cycles_for_range
from app.services.import_export_service import import_students_and_schedules
from app.services.payment_service import record_payment


def setup_basic():
    t = Therapist(name="T")
    s = Student(name="S", contract_hours_per_week=2)
    db.session.add_all([t, s])
    db.session.flush()
    s.assigned_therapist_id = t.id
    db.session.add(RegularSchedule(student_id=s.id, therapist_id=t.id, day_of_week=0, start_time=time(9, 0), duration_hours=1))
    db.session.commit()
    return s, t


def mark_rendered(student_id: int | None = None):
    q = AttendanceSession.query
    if student_id:
        q = q.filter_by(student_id=student_id)
    for sess in q.all():
        sess.status = "Present"
    db.session.commit()


def test_monthly_session_generation(session):
    setup_basic()
    count = generate_monthly_sessions(2026, 1)
    assert count > 0


def test_makeup_session_creation(session):
    s, t = setup_basic()
    session_obj = create_makeup_session(s.id, t.id, date(2026, 1, 2), time(10, 0), 1.0)
    assert session_obj.session_type == "makeup"
    assert session_obj.status == ""


def test_weekly_hours_calculation(session):
    s, t = setup_basic()
    generate_monthly_sessions(2026, 1)
    mark_rendered(s.id)
    student_hours = weekly_student_hours(date(2026, 1, 1), date(2026, 1, 7))
    therapist_hours = weekly_therapist_hours(date(2026, 1, 1), date(2026, 1, 7))
    assert student_hours[s.id] == therapist_hours[t.id]


def test_billing_cycle_generation_and_due_date(session):
    cycles = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 31))
    assert len(cycles) == 3
    assert (cycles[0].due_date - cycles[0].issue_date).days == 5


def test_weekday_weekend_rate_billing(session):
    t = Therapist(name="T2")
    s = Student(name="S2", contract_hours_per_week=2, assigned_therapist=t)
    db.session.add_all([t, s])
    db.session.flush()
    db.session.add(RegularSchedule(student_id=s.id, therapist_id=t.id, day_of_week=4, start_time=time(9, 0), duration_hours=1))
    db.session.add(RegularSchedule(student_id=s.id, therapist_id=t.id, day_of_week=5, start_time=time(9, 0), duration_hours=1))
    db.session.commit()
    generate_monthly_sessions(2026, 1)
    mark_rendered(s.id)
    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advices = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)
    advice = advices[0]
    assert advice.subtotal_sessions >= WEEKDAY_RATE


def test_required_deposit_stops_after_four_cycles(session):
    s, _ = setup_basic()
    generate_monthly_sessions(2026, 1)
    mark_rendered(s.id)
    cycles = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 3, 31))
    for c in cycles[:5]:
        generate_billing_advices_for_cycle(c.id, student_id=s.id)
    advices = BillingAdvice.query.filter_by(student_id=s.id).order_by(BillingAdvice.id).all()
    non_zero = [a for a in advices if a.required_deposit_charge > 0]
    assert len(non_zero) <= 4


def test_assessment_deposit_tracking(session):
    s, _ = setup_basic()
    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    assert advice.assessment_deposit_charge == 2500.0


def test_partial_payment_allocation(session):
    s, _ = setup_basic()
    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    before = advice.total_due
    record_payment(s.id, 1000, date(2026, 1, 16))
    refreshed = BillingAdvice.query.get(advice.id)
    assert refreshed.total_due == max(before - 1000, 0)


def test_overpayment_carry_forward(session):
    s, _ = setup_basic()
    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    record_payment(s.id, advice.total_due + 500, date(2026, 1, 16))
    db.session.refresh(s)
    assert s.overpayment_credit == 500


def test_import_validates_required_columns(session, tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Student Name", "Therapist"])
    ws.append(["Example", "Therapist A"])
    path = tmp_path / "bad_import.xlsx"
    wb.save(path)

    with pytest.raises(ValueError):
        import_students_and_schedules(str(path))
