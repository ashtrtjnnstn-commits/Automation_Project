from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for

from app.models import AdminAttendance, AdminStaff, AttendanceSession, BillingAdvice, BillingCycle, Student, Therapist, db
from app.services.admin_service import admin_hours_summary
from app.services.attendance_service import (
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
    import_students_and_schedules,
)
from app.services.payment_service import record_payment
from app.utils.date_utils import week_bounds

web_bp = Blueprint("web", __name__)


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
    )


@web_bp.route("/attendance/daily")
def daily_schedule():
    selected = datetime.strptime(request.args.get("date", date.today().isoformat()), "%Y-%m-%d").date()
    sessions = AttendanceSession.query.filter_by(session_date=selected).order_by(AttendanceSession.start_time).all()
    return render_template("daily_schedule.html", selected=selected, sessions=sessions)


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


@web_bp.route("/session/<int:session_id>/status", methods=["POST"])
def session_status(session_id: int):
    status = request.form["status"]
    notes = request.form.get("notes", "")
    update_session_status(session_id, status, notes)
    flash("Session updated.", "success")
    return redirect(request.referrer or url_for("web.attendance_month"))


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
    if request.method == "POST":
        start = datetime.strptime(request.form["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(request.form["end_date"], "%Y-%m-%d").date()
        cycles = generate_billing_cycles_for_range(start, end)
        for c in cycles:
            generate_billing_advices_for_cycle(c.id)
        flash(f"Generated {len(cycles)} cycle(s) and billing advice.", "success")
        return redirect(url_for("web.billing_page"))
    cycles = BillingCycle.query.order_by(BillingCycle.start_date.desc()).limit(10).all()
    return render_template("billing.html", cycles=cycles)


@web_bp.route("/payments", methods=["GET", "POST"])
def payments_page():
    if request.method == "POST":
        record_payment(
            student_id=int(request.form["student_id"]),
            amount=float(request.form["amount"]),
            payment_date=datetime.strptime(request.form["payment_date"], "%Y-%m-%d").date(),
            notes=request.form.get("notes", ""),
        )
        flash("Payment recorded.", "success")
        return redirect(url_for("web.payments_page"))
    return render_template("payments.html", students=Student.query.all())


@web_bp.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "POST":
        file = request.files["file"]
        save_path = Path("data") / file.filename
        save_path.parent.mkdir(exist_ok=True)
        file.save(save_path)
        try:
            inserted = import_students_and_schedules(str(save_path))
            flash(f"Imported {inserted} schedule rows.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.import_page"))
    return render_template("import.html")


@web_bp.route("/export/<string:kind>")
def export_page(kind: str):
    Path("exports").mkdir(exist_ok=True)
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
