from __future__ import annotations

from datetime import date, time

import pytest
from openpyxl import Workbook, load_workbook

from app.models import AdminStaff, AttendanceSession, BillingAdvice, Payment, RegularSchedule, SessionOverride, Student, Therapist, db
from app.services.attendance_service import create_makeup_session, generate_monthly_sessions, missed_recovery_summary, weekly_student_hours, weekly_therapist_hours
from app.services.billing_service import WEEKDAY_RATE, generate_billing_advices_for_cycle, generate_billing_cycles_for_range
from app.services.import_export_service import import_students_and_schedules
from app.services.import_export_service import (
    export_assessment_deposit_payment_history,
    export_billing_advices,
    export_required_deposit_payment_history,
)
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


def test_required_deposit_charge_respects_paid_amount(session):
    s, _ = setup_basic()
    s.required_deposit_total = 10000
    s.required_deposit_billed = 2500
    s.required_deposit_paid = 9500
    db.session.commit()

    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    assert advice.required_deposit_charge == 500


def test_assessment_deposit_charge_respects_paid_amount(session):
    s, _ = setup_basic()
    s.assessment_deposit_total = 5000
    s.assessment_deposit_billed = 0
    s.assessment_deposit_paid = 4900
    db.session.commit()

    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    assert advice.assessment_deposit_charge == 100


def test_billing_excludes_replaced_original_session(session):
    s, t = setup_basic()
    original = AttendanceSession(
        student_id=s.id,
        therapist_id=t.id,
        session_date=date(2026, 1, 5),
        start_time=time(9, 0),
        end_time=time(10, 0),
        duration_hours=1,
        status="Present",
        source_type="generated",
    )
    replacement = AttendanceSession(
        student_id=s.id,
        therapist_id=t.id,
        session_date=date(2026, 1, 6),
        start_time=time(9, 0),
        end_time=time(10, 0),
        duration_hours=1,
        status="Rescheduled",
        source_type="manual",
    )
    db.session.add_all([original, replacement])
    db.session.flush()
    db.session.add(SessionOverride(original_session_id=original.id, new_session_id=replacement.id, override_type="reschedule"))
    db.session.commit()

    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    assert advice.subtotal_sessions == 550.0


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


def test_daily_schedule_updates_attendance_record(session, client):
    s, _ = setup_basic()
    generate_monthly_sessions(2026, 1)
    target = AttendanceSession.query.filter_by(student_id=s.id).first()

    response = client.post(
        "/attendance/daily",
        data={
            "selected_date": target.session_date.isoformat(),
            "session_ids": [str(target.id)],
            f"status_{target.id}": "Present",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    db.session.refresh(target)
    assert target.status == "Present"


def test_monthly_attendance_reflects_daily_status(session, client):
    s, _ = setup_basic()
    generate_monthly_sessions(2026, 1)
    target = AttendanceSession.query.filter_by(student_id=s.id).first()
    target.status = "Absent"
    db.session.commit()

    response = client.get(f"/attendance?year=2026&month=1&student_id={s.id}")
    assert response.status_code == 200
    assert b"Absent" in response.data


def test_payment_dashboard_record_creation(session, client):
    s, _ = setup_basic()
    admin = AdminStaff(name="Receiver A")
    db.session.add(admin)
    db.session.commit()

    response = client.post(
        "/payments",
        data={
            "payment_date": "2026-01-10",
            "client_guardian_name": "Juan Dela Cruz",
            "student_id": str(s.id),
            "purpose": "Therapy",
            "billing_period_start": "2026-01-01",
            "billing_period_end": "2026-01-15",
            "total_hours_rendered": "2",
            "amount": "1000",
            "received_by_admin_id": str(admin.id),
            "overpayment_amount": "12.5",
            "balance_after_payment": "88.0",
            "mode_of_transfer": "GCash",
            "notes": "test",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    payment = Payment.query.order_by(Payment.id.desc()).first()
    assert payment.client_guardian_name == "Juan Dela Cruz"
    assert payment.mode_of_transfer == "GCash"
    assert payment.overpayment_amount == 12.5
    assert payment.balance_after_payment == 88.0


def test_monthly_payment_archive_retrieval(session, client):
    s, _ = setup_basic()
    db.session.add(Payment(student_id=s.id, payment_date=date(2026, 1, 10), amount=500, client_guardian_name="A"))
    db.session.commit()

    res_archive = client.post("/payments/tracker", data={"action": "archive", "archive_month": "1", "archive_year": "2026"}, follow_redirects=True)
    assert res_archive.status_code == 200

    res_view = client.get("/payments/tracker?view=archived&month=1&year=2026")
    assert res_view.status_code == 200
    assert b"2026-01-10" in res_view.data


def test_billing_page_requires_student_selection(session, client):
    response = client.post(
        "/billing",
        data={"start_date": "2026-01-01", "end_date": "2026-01-15", "student_id": ""},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Please select a student first." in response.data


def test_payments_tracker_filters_current_month(session, client):
    s, _ = setup_basic()
    db.session.add(Payment(student_id=s.id, payment_date=date(2026, 1, 10), amount=500, client_guardian_name="A", is_archived=False))
    db.session.add(Payment(student_id=s.id, payment_date=date(2026, 2, 10), amount=700, client_guardian_name="B", is_archived=False))
    db.session.commit()

    res = client.get("/payments/tracker?view=active&month=1&year=2026")
    assert res.status_code == 200
    assert b"2026-01-10" in res.data
    assert b"2026-02-10" not in res.data


def test_edit_payment_entry(session, client):
    s, _ = setup_basic()
    admin = AdminStaff(name="Receiver Edit")
    db.session.add(admin)
    db.session.flush()
    p = Payment(student_id=s.id, payment_date=date(2026, 1, 10), amount=500, client_guardian_name="Before", mode_of_transfer="Cash")
    db.session.add(p)
    db.session.commit()

    res = client.post(
        f"/payments/{p.id}/edit",
        data={
            "payment_date": "2026-01-12",
            "client_guardian_name": "After",
            "student_id": str(s.id),
            "purpose": "Partial Payment",
            "billing_period_start": "2026-01-01",
            "billing_period_end": "2026-01-15",
            "total_hours_rendered": "3",
            "amount": "750",
            "received_by_admin_id": str(admin.id),
            "overpayment_amount": "10",
            "balance_after_payment": "20",
            "mode_of_transfer": "GCash",
            "notes": "edited",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    db.session.refresh(p)
    assert p.payment_date == date(2026, 1, 12)
    assert p.client_guardian_name == "After"
    assert p.purpose == "Partial Payment"
    assert p.amount == 750
    assert p.mode_of_transfer == "GCash"


def test_master_data_student_crud_page(session, client):
    res_create = client.post(
        "/master-data/students",
        data={"action": "create", "name": "New Student", "contract_hours_per_week": "4", "overpayment_credit": "0", "active": "1"},
        follow_redirects=True,
    )
    assert res_create.status_code == 200
    student = Student.query.filter_by(name="New Student").first()
    assert student is not None

    res_edit = client.post(
        "/master-data/students",
        data={"action": "edit", "student_id": str(student.id), "name": "Updated Student", "contract_hours_per_week": "5", "overpayment_credit": "1.5", "active": "0"},
        follow_redirects=True,
    )
    assert res_edit.status_code == 200
    db.session.refresh(student)
    assert student.name == "Updated Student"
    assert student.active is False


def test_master_data_schedule_validation_end_time(session, client):
    s, t = setup_basic()
    response = client.post(
        "/master-data/schedules",
        data={
            "action": "create",
            "student_id": str(s.id),
            "therapist_id": str(t.id),
            "day_of_week": "1",
            "start_time": "10:00",
            "end_time": "09:00",
            "active": "1",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"End time must be after start time." in response.data


def test_weekly_report_archive_creation_and_view(session, client):
    s, _ = setup_basic()
    generate_monthly_sessions(2026, 1)
    mark_rendered(s.id)

    res = client.post("/reports/weekly", data={"date": "2026-01-05", "action": "archive_week", "note": "snapshot"}, follow_redirects=True)
    assert res.status_code == 200
    assert b"Weekly report archived." in res.data

    # locate archive link and open list page
    list_res = client.get("/reports/weekly?date=2026-01-05")
    assert list_res.status_code == 200
    assert b"Archived Weekly Reports" in list_res.data


def test_weekly_report_archive_prevents_duplicate(session, client):
    s, _ = setup_basic()
    generate_monthly_sessions(2026, 1)
    mark_rendered(s.id)

    client.post("/reports/weekly", data={"date": "2026-01-05", "action": "archive_week", "note": "a"}, follow_redirects=True)
    res2 = client.post("/reports/weekly", data={"date": "2026-01-05", "action": "archive_week", "note": "b"}, follow_redirects=True)
    assert res2.status_code == 200
    assert b"already archived" in res2.data


def test_required_deposit_export_includes_expected_row_and_columns_for_student(session, tmp_path):
    s, _ = setup_basic()
    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    record_payment(s.id, advice.required_deposit_charge, date(2026, 1, 16), client_guardian_name="Guardian A", purpose="Required Deposit", mode_of_transfer="GCash")

    output = export_required_deposit_payment_history(str(tmp_path / "required_one.xlsx"), student_id=s.id)
    wb = load_workbook(output)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    assert "Payment Date" in headers
    assert "Client/Guardian" in headers
    assert "Allocation Amount" in headers
    assert ws.max_row >= 2
    assert ws.cell(row=2, column=headers.index("Student") + 1).value == s.name


def test_required_deposit_export_supports_all_students(session, tmp_path):
    s1, _ = setup_basic()
    s2 = Student(name="S-2", contract_hours_per_week=2)
    db.session.add(s2)
    db.session.commit()

    c = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    a1 = generate_billing_advices_for_cycle(c.id, student_id=s1.id)[0]
    a2 = generate_billing_advices_for_cycle(c.id, student_id=s2.id)[0]
    record_payment(s1.id, a1.required_deposit_charge, date(2026, 1, 16), purpose="Required Deposit")
    record_payment(s2.id, a2.required_deposit_charge, date(2026, 1, 16), purpose="Required Deposit")

    output = export_required_deposit_payment_history(str(tmp_path / "required_all.xlsx"))
    ws = load_workbook(output).active
    names = {ws.cell(row=i, column=4).value for i in range(2, ws.max_row + 1)}
    assert s1.name in names
    assert s2.name in names


def test_assessment_deposit_export_includes_expected_row_and_columns_for_student(session, tmp_path):
    s, _ = setup_basic()
    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    advice = generate_billing_advices_for_cycle(cycle.id, student_id=s.id)[0]
    record_payment(s.id, advice.assessment_deposit_charge, date(2026, 1, 16), client_guardian_name="Guardian B", purpose="Assessment Deposit", mode_of_transfer="Bank Transfer")

    output = export_assessment_deposit_payment_history(str(tmp_path / "assessment_one.xlsx"), student_id=s.id)
    wb = load_workbook(output)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    assert "Payment Date" in headers
    assert "Mode of Transfer" in headers
    assert "Allocation Amount" in headers
    assert ws.max_row >= 2
    assert ws.cell(row=2, column=headers.index("Student") + 1).value == s.name


def test_assessment_deposit_export_supports_all_students(session, tmp_path):
    s1, _ = setup_basic()
    s2 = Student(name="S-3", contract_hours_per_week=2)
    db.session.add(s2)
    db.session.commit()

    c = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    a1 = generate_billing_advices_for_cycle(c.id, student_id=s1.id)[0]
    a2 = generate_billing_advices_for_cycle(c.id, student_id=s2.id)[0]
    record_payment(s1.id, a1.assessment_deposit_charge, date(2026, 1, 16), purpose="Assessment Deposit")
    record_payment(s2.id, a2.assessment_deposit_charge, date(2026, 1, 16), purpose="Assessment Deposit")

    output = export_assessment_deposit_payment_history(str(tmp_path / "assessment_all.xlsx"))
    ws = load_workbook(output).active
    names = {ws.cell(row=i, column=4).value for i in range(2, ws.max_row + 1)}
    assert s1.name in names
    assert s2.name in names


def test_billing_export_includes_regular_and_makeup_hours_columns(session, tmp_path):
    s, t = setup_basic()
    original = AttendanceSession(
        student_id=s.id,
        therapist_id=t.id,
        session_date=date(2026, 1, 5),
        start_time=time(9, 0),
        end_time=time(10, 0),
        duration_hours=1,
        status="Cancelled",
        session_type="regular",
        source_type="generated",
    )
    makeup = AttendanceSession(
        student_id=s.id,
        therapist_id=t.id,
        session_date=date(2026, 1, 6),
        start_time=time(9, 0),
        end_time=time(10, 0),
        duration_hours=1,
        status="Present",
        session_type="makeup",
        source_type="manual",
    )
    db.session.add_all([original, makeup])
    db.session.flush()
    db.session.add(SessionOverride(original_session_id=original.id, new_session_id=makeup.id, override_type="makeup"))
    db.session.commit()

    cycle = generate_billing_cycles_for_range(date(2026, 1, 1), date(2026, 1, 15))[0]
    generate_billing_advices_for_cycle(cycle.id, student_id=s.id)

    output = export_billing_advices(str(tmp_path / "billing.xlsx"))
    ws = load_workbook(output).active
    headers = [c.value for c in ws[1]]
    assert "Regular Rendered Hours" in headers
    assert "Make-up Rendered Hours" in headers
    reg_col = headers.index("Regular Rendered Hours") + 1
    make_col = headers.index("Make-up Rendered Hours") + 1
    assert ws.cell(row=2, column=reg_col).value == 0
    assert ws.cell(row=2, column=make_col).value == 1


def test_missed_hours_summary_computation(session):
    s, t = setup_basic()
    missed = AttendanceSession(
        student_id=s.id,
        therapist_id=t.id,
        session_date=date(2026, 1, 5),
        start_time=time(9, 0),
        end_time=time(10, 0),
        duration_hours=1,
        status="Absent",
        session_type="regular",
        source_type="generated",
    )
    recovered = AttendanceSession(
        student_id=s.id,
        therapist_id=t.id,
        session_date=date(2026, 1, 6),
        start_time=time(9, 0),
        end_time=time(10, 0),
        duration_hours=0.5,
        status="Present",
        session_type="makeup",
        source_type="manual",
    )
    db.session.add_all([missed, recovered])
    db.session.flush()
    db.session.add(SessionOverride(original_session_id=missed.id, new_session_id=recovered.id, override_type="makeup"))
    db.session.commit()

    summary = missed_recovery_summary(student_id=s.id)
    assert summary["missed_hours"] == 1.0
    assert summary["recovered_makeup_hours"] == 0.5
    assert summary["remaining_missed_hours"] == 0.5
