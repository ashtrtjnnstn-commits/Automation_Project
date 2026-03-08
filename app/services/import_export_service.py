from __future__ import annotations

from datetime import datetime, date
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.models import (
    AdminAttendance,
    AttendanceSession,
    BillingAdvice,
    Payment,
    RegularSchedule,
    Student,
    Therapist,
    db,
)
from app.services.attendance_service import weekly_therapist_hours

DAY_MAP = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def import_students_and_schedules(path: str, column_map: dict[str, str] | None = None) -> int:
    """Import students and regular schedules from a single worksheet.

    Raises ValueError when required columns are missing.
    """
    wb = load_workbook(path)
    ws = wb.active
    header = [str(c.value).strip() if c.value else "" for c in ws[1]]
    mapped = column_map or {
        "student_name": "Student Name",
        "therapist_name": "Therapist",
        "day_of_week": "Day",
        "start_time": "Start Time",
        "duration_hours": "Duration Hours",
        "contract_hours": "Contract Hours",
    }

    missing = [sheet_col for sheet_col in mapped.values() if sheet_col not in header]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    index = {k: header.index(v) for k, v in mapped.items()}
    inserted = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[index["student_name"]]:
            continue

        therapist_name = str(row[index["therapist_name"]]).strip()
        therapist = Therapist.query.filter_by(name=therapist_name).first() or Therapist(name=therapist_name)
        db.session.add(therapist)
        db.session.flush()

        student_name = str(row[index["student_name"]]).strip()
        student = Student.query.filter_by(name=student_name).first()
        if not student:
            student = Student(
                name=student_name,
                assigned_therapist_id=therapist.id,
                contract_hours_per_week=float(row[index["contract_hours"]] or 1),
            )
            db.session.add(student)
            db.session.flush()

        day = DAY_MAP[str(row[index["day_of_week"]]).strip().lower()]
        st_raw = row[index["start_time"]]
        start_time = st_raw if hasattr(st_raw, "hour") else datetime.strptime(str(st_raw), "%H:%M").time()
        duration = float(row[index["duration_hours"]])
        schedule = RegularSchedule(
            student_id=student.id,
            therapist_id=therapist.id,
            day_of_week=day,
            start_time=start_time,
            duration_hours=duration,
        )
        db.session.add(schedule)
        inserted += 1
    db.session.commit()
    return inserted


def export_attendance_summary(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    ws.append(["Date", "Student", "Therapist", "Start", "Duration", "Status", "Source"])
    for s in AttendanceSession.query.order_by(AttendanceSession.session_date).all():
        ws.append([
            s.session_date.isoformat(),
            s.student.name,
            s.therapist.name,
            s.start_time.strftime("%H:%M"),
            s.duration_hours,
            s.status,
            s.source_type,
        ])
    wb.save(path)
    return path


def export_admin_attendance(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Admin Attendance"
    ws.append(["Date", "Admin", "Status", "Shift", "Hours"])
    for a in AdminAttendance.query.order_by(AdminAttendance.attendance_date).all():
        ws.append([a.attendance_date.isoformat(), a.admin.name, a.status, a.shift_label, a.hours_worked])
    wb.save(path)
    return path


def export_therapist_weekly_hours(path: str, start_date: date, end_date: date) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Therapist Weekly Hours"
    ws.append(["Start", "End", "Therapist", "Hours"])
    totals = weekly_therapist_hours(start_date, end_date)
    for therapist_id, hours in totals.items():
        therapist = Therapist.query.get(therapist_id)
        ws.append([start_date.isoformat(), end_date.isoformat(), therapist.name if therapist else therapist_id, hours])
    wb.save(path)
    return path


def export_billing_advices(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Billing Advice"
    ws.append(["Student", "Cycle Start", "Cycle End", "Issue", "Due", "Subtotal", "Old Balance", "Required Deposit", "Assessment Deposit", "Total Due", "Status"])
    for advice in BillingAdvice.query.order_by(BillingAdvice.id).all():
        cycle = advice.billing_cycle
        ws.append([
            advice.student.name,
            cycle.start_date.isoformat(),
            cycle.end_date.isoformat(),
            cycle.issue_date.isoformat(),
            cycle.due_date.isoformat(),
            advice.subtotal_sessions,
            advice.old_balance,
            advice.required_deposit_charge,
            advice.assessment_deposit_charge,
            advice.total_due,
            advice.status,
        ])
    wb.save(path)
    return path


def export_payment_ledger(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Payments"
    ws.append(["Payment ID", "Date", "Student", "Amount", "Allocation Type", "Allocation Amount", "Advice ID", "Notes"])
    for p in Payment.query.order_by(Payment.payment_date, Payment.id).all():
        if p.allocations:
            for alloc in p.allocations:
                ws.append([
                    p.id,
                    p.payment_date.isoformat(),
                    p.student.name,
                    p.amount,
                    alloc.allocation_type,
                    alloc.amount,
                    alloc.billing_advice_id,
                    p.notes,
                ])
        else:
            ws.append([p.id, p.payment_date.isoformat(), p.student.name, p.amount, "", 0, "", p.notes])
    wb.save(path)
    return path
