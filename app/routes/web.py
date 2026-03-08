from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from app.models import (
    AdminAttendance,
    AdminStaff,
    AssessmentDepositLedger,
    AttendanceSession,
    BillingAdvice,
    BillingCycle,
    BillingLineItem,
    Payment,
    PaymentAllocation,
    RegularSchedule,
    RequiredDepositLedger,
    Student,
    Therapist,
    db,
)
from app.services.admin_service import admin_hours_summary
from app.services.attendance_service import (
    RENDERED_STATUSES,
    create_makeup_session,
    generate_monthly_sessions,
    get_month_sessions,
    update_session_status,
    weekly_student_hours,
    weekly_therapist_hours,
)
from app.services.billing_service import due_summary, generate_billing_advices_for_cycle, generate_billing_cycles_for_range
from app.services.import_export_service import (
    export_admin_attendance,
    export_attendance_summary,
    export_billing_advices,
    export_payment_ledger,
    export_therapist_weekly_hours,
    import_admin_staff,
    import_students_and_schedules,
    import_therapists,
)
from app.services.payment_service import archive_payments, record_payment
from app.utils.date_utils import week_bounds

web_bp = Blueprint("web", __name__)

ATTENDANCE_STATUSES = ["Present", "Absent", "Cancelled", "Make-up", "Rescheduled", "No Show"]


@web_bp.route("/")
def dashboard():
    today = date.today()
    due = due_summary(today)
    recent_advices = BillingAdvice.query.order_by(BillingAdvice.created_at.desc()).limit(10).all()
    return render_template("dashboard.html", due=due, today=today, recent_advices=recent_advices)


@web_bp.route("/attendance", methods=["GET", "POST"])
def attendance_month():
    today = date.today()
    year = int(request.values.get("year", today.year))
    month = int(request.values.get("month", today.month))
    therapist_id = request.values.get("therapist_id", type=int)
    student_id = request.values.get("student_id", type=int)

    if request.method == "POST" and request.form.get("action") == "generate":
        count = generate_monthly_sessions(year, month)
        flash(f"Generated {count} sessions.", "success")
        return redirect(url_for("web.attendance_month", year=year, month=month))

    sessions = get_month_sessions(year, month, therapist_id=therapist_id, student_id=student_id)
    return render_template(
        "attendance_month.html",
        sessions=sessions,
        year=year,
        month=month,
        therapists=Therapist.query.filter_by(active=True).all(),
        students=Student.query.filter_by(active=True).all(),
        rendered_statuses=RENDERED_STATUSES,
    )


@web_bp.route("/attendance/daily", methods=["GET", "POST"])
def daily_schedule():
    if request.method == "POST":
        session_ids = request.form.getlist("session_ids")
        updated = 0
        for raw_id in session_ids:
            status = request.form.get(f"status_{raw_id}", "")
            session = AttendanceSession.query.get(int(raw_id))
            if not session:
                continue
            if session.status != status:
                update_session_status(session.id, status)
                updated += 1
        flash(f"Updated {updated} session(s).", "success")
        return redirect(url_for("web.daily_schedule", date=request.form["selected_date"]))

    selected = datetime.strptime(request.args.get("date", date.today().isoformat()), "%Y-%m-%d").date()
    sessions = AttendanceSession.query.filter_by(session_date=selected).order_by(AttendanceSession.start_time).all()
    return render_template("daily_schedule.html", selected=selected, sessions=sessions, attendance_statuses=ATTENDANCE_STATUSES)


@web_bp.route("/students/<int:student_id>")
def student_profile(student_id: int):
    student = Student.query.get_or_404(student_id)
    sessions = AttendanceSession.query.filter_by(student_id=student.id).order_by(AttendanceSession.session_date.desc()).limit(20).all()
    open_advices = BillingAdvice.query.filter_by(student_id=student.id, status="Open").all()
    return render_template("student_profile.html", student=student, sessions=sessions, open_advices=open_advices)


@web_bp.route("/therapists/<int:therapist_id>")
def therapist_profile(therapist_id: int):
    therapist = Therapist.query.get_or_404(therapist_id)
    sessions = AttendanceSession.query.filter_by(therapist_id=therapist.id).order_by(AttendanceSession.session_date.desc()).limit(20).all()
    return render_template("therapist_profile.html", therapist=therapist, sessions=sessions)


@web_bp.route("/makeup", methods=["GET", "POST"])
def makeup_editor():
    if request.method == "POST":
        create_makeup_session(
            student_id=int(request.form["student_id"]),
            therapist_id=int(request.form["therapist_id"]),
            session_date=datetime.strptime(request.form["session_date"], "%Y-%m-%d").date(),
            start_time=datetime.strptime(request.form["start_time"], "%H:%M").time(),
            duration_hours=float(request.form["duration_hours"]),
            override_type=request.form.get("override_type", "makeup"),
            original_session_id=request.form.get("original_session_id", type=int),
            notes=request.form.get("notes", ""),
        )
        flash("Make-up session added.", "success")
        return redirect(url_for("web.makeup_editor"))

    return render_template("makeup_editor.html", students=Student.query.all(), therapists=Therapist.query.all())


@web_bp.route("/reports/weekly")
def weekly_reports():
    base = datetime.strptime(request.args.get("date", date.today().isoformat()), "%Y-%m-%d").date()
    start, end = week_bounds(base)
    return render_template(
        "weekly_reports.html",
        start=start,
        end=end,
        student_hours=weekly_student_hours(start, end),
        therapist_hours=weekly_therapist_hours(start, end),
        admin_hours=admin_hours_summary(start, end),
        students={s.id: s for s in Student.query.all()},
        therapists={t.id: t for t in Therapist.query.all()},
        admins={a.id: a for a in AdminStaff.query.all()},
    )


@web_bp.route("/reports/export")
def export_reports_page():
    return render_template("export_reports.html")


@web_bp.route("/admin-attendance", methods=["GET", "POST"])
def admin_attendance_page():
    if request.method == "POST":
        row = AdminAttendance(
            admin_id=int(request.form["admin_id"]),
            attendance_date=datetime.strptime(request.form["attendance_date"], "%Y-%m-%d").date(),
            status=request.form["status"],
            shift_label=request.form.get("shift_label", ""),
            hours_worked=float(request.form.get("hours_worked", 0) or 0),
            notes=request.form.get("notes", ""),
        )
        db.session.add(row)
        db.session.commit()
        flash("Admin attendance saved.", "success")
        return redirect(url_for("web.admin_attendance_page"))
    records = AdminAttendance.query.order_by(AdminAttendance.attendance_date.desc()).limit(30).all()
    return render_template("admin_attendance.html", admins=AdminStaff.query.all(), records=records)


@web_bp.route("/billing", methods=["GET", "POST"])
def billing_page():
    generated_advices: list[BillingAdvice] = []
    selected_student_id = request.values.get("student_id", type=int)

    if request.method == "POST":
        selected_student_id = request.form.get("student_id", type=int)
        if not selected_student_id:
            flash("Please select a student first.", "error")
            return redirect(url_for("web.billing_page"))

        start = datetime.strptime(request.form["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(request.form["end_date"], "%Y-%m-%d").date()
        cycles = generate_billing_cycles_for_range(start, end)
        for c in cycles:
            generated_advices.extend(generate_billing_advices_for_cycle(c.id, student_id=selected_student_id))
        flash(f"Generated billing for {len(generated_advices)} advice record(s).", "success")

    cycles = BillingCycle.query.order_by(BillingCycle.start_date.desc()).limit(10).all()
    selected_student = Student.query.get(selected_student_id) if selected_student_id else None
    student_advices = []
    if selected_student_id:
        student_advices = BillingAdvice.query.filter_by(student_id=selected_student_id).order_by(BillingAdvice.id.desc()).limit(20).all()

    return render_template(
        "billing.html",
        cycles=cycles,
        students=Student.query.filter_by(active=True).all(),
        selected_student_id=selected_student_id,
        selected_student=selected_student,
        generated_advices=generated_advices,
        student_advices=student_advices,
    )


@web_bp.route("/payments", methods=["GET", "POST"])
def payments_page():
    if request.method == "POST":
        billing_period_start = request.form.get("billing_period_start")
        billing_period_end = request.form.get("billing_period_end")
        payment = record_payment(
            student_id=int(request.form["student_id"]),
            amount=float(request.form["amount"]),
            payment_date=datetime.strptime(request.form["payment_date"], "%Y-%m-%d").date(),
            notes=request.form.get("notes", ""),
            client_guardian_name=request.form.get("client_guardian_name", ""),
            purpose=request.form.get("purpose", "Therapy"),
            billing_period_start=datetime.strptime(billing_period_start, "%Y-%m-%d").date() if billing_period_start else None,
            billing_period_end=datetime.strptime(billing_period_end, "%Y-%m-%d").date() if billing_period_end else None,
            total_hours_rendered=float(request.form.get("total_hours_rendered", 0) or 0),
            received_by_admin_id=request.form.get("received_by_admin_id", type=int),
            mode_of_transfer=request.form.get("mode_of_transfer", "Cash"),
            manual_overpayment=request.form.get("overpayment_amount", type=float),
            manual_balance=request.form.get("balance_after_payment", type=float),
        )
        flash(f"Payment recorded (ID {payment.id}).", "success")
        return redirect(url_for("web.payments_tracker"))
    return render_template("payments.html", students=Student.query.all(), admins=AdminStaff.query.filter_by(active=True).all())


@web_bp.route("/payments/tracker", methods=["GET", "POST"])
def payments_tracker():
    if request.method == "POST" and request.form.get("action") == "archive":
        month = int(request.form["archive_month"])
        year = int(request.form["archive_year"])
        count = archive_payments(month=month, year=year)
        flash(f"Archived {count} payment record(s) for {year}-{month:02d}.", "success")
        return redirect(url_for("web.payments_tracker", view="active"))

    today = date.today()
    view = request.args.get("view", "active")
    month = request.args.get("month", type=int) or today.month
    year = request.args.get("year", type=int) or today.year

    query = Payment.query
    if view == "archived":
        query = query.filter(Payment.is_archived.is_(True))
        query = query.filter(Payment.archive_month == month, Payment.archive_year == year)
    else:
        query = query.filter(Payment.is_archived.is_(False))
        query = query.filter(
            Payment.payment_date >= date(year, month, 1),
            Payment.payment_date < (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)),
        )

    payments = query.order_by(Payment.payment_date.desc(), Payment.id.desc()).all()
    archived_groups = (
        db.session.query(Payment.archive_year, Payment.archive_month)
        .filter(Payment.is_archived.is_(True))
        .distinct()
        .order_by(Payment.archive_year.desc(), Payment.archive_month.desc())
        .all()
    )
    return render_template(
        "payments_tracker.html",
        payments=payments,
        view=view,
        month=month,
        year=year,
        archived_groups=archived_groups,
    )


@web_bp.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "POST":
        action = request.form.get("action", "import")

        if action == "clear":
            target = request.form["clear_target"]
            confirmation = request.form.get("confirm_text", "")
            if confirmation != "CLEAR":
                flash("Type CLEAR to confirm data cleanup.", "error")
                return redirect(url_for("web.import_page"))

            if target == "students":
                BillingLineItem.query.delete()
                PaymentAllocation.query.delete()
                Payment.query.delete()
                RequiredDepositLedger.query.delete()
                AssessmentDepositLedger.query.delete()
                BillingAdvice.query.delete()
                AttendanceSession.query.delete()
                RegularSchedule.query.delete()
                Student.query.delete()
            elif target == "therapists":
                AttendanceSession.query.delete()
                RegularSchedule.query.delete()
                for student in Student.query.all():
                    student.assigned_therapist_id = None
                Therapist.query.delete()
            elif target == "admins":
                AdminAttendance.query.delete()
                AdminStaff.query.delete()
            db.session.commit()
            flash(f"Cleared imported {target} data.", "success")
            return redirect(url_for("web.import_page"))

        file = request.files["file"]
        save_path = Path("data") / file.filename
        save_path.parent.mkdir(exist_ok=True)
        file.save(save_path)
        dataset = request.form.get("dataset", "students_schedules")
        try:
            if dataset == "students_schedules":
                inserted = import_students_and_schedules(str(save_path))
            elif dataset == "admins":
                inserted = import_admin_staff(str(save_path))
            else:
                inserted = import_therapists(str(save_path))
            flash(f"Imported {inserted} row(s) for {dataset}.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.import_page"))
    return render_template("import.html")


@web_bp.route("/export/<string:kind>")
def export_page(kind: str):
    Path("exports").mkdir(exist_ok=True)
    try:
        if kind == "attendance":
            path = export_attendance_summary("exports/attendance.xlsx")
        elif kind == "admin":
            path = export_admin_attendance("exports/admin_attendance.xlsx")
        elif kind == "billing":
            path = export_billing_advices("exports/billing_advices.xlsx")
        elif kind == "payments":
            path = export_payment_ledger("exports/payment_ledger.xlsx")
        elif kind == "therapist-weekly":
            today = date.today()
            start, end = week_bounds(today)
            path = export_therapist_weekly_hours("exports/therapist_weekly_hours.xlsx", start, end)
        else:
            flash("Unknown export type", "error")
            return redirect(url_for("web.dashboard"))
        return send_file(path, as_attachment=True)
    except FileNotFoundError:
        flash("Export file could not be created. Please try again.", "error")
        return redirect(url_for("web.export_reports_page"))
