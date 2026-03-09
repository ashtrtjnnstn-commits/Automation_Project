from __future__ import annotations

from datetime import date, datetime, time, timedelta
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
    WeeklyReportArchive,
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
from app.services.weekly_archive_service import archive_weekly_report, get_archive_sections
from app.utils.date_utils import week_bounds

web_bp = Blueprint("web", __name__)

ATTENDANCE_STATUSES = ["Present", "Absent", "Cancelled", "Make-up", "Rescheduled", "No Show", "Non-billable"]


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


@web_bp.route("/reports/weekly", methods=["GET", "POST"])
def weekly_reports():
    base = datetime.strptime(request.values.get("date", date.today().isoformat()), "%Y-%m-%d").date()
    start, end = week_bounds(base)

    if request.method == "POST" and request.form.get("action") == "archive_week":
        note = request.form.get("note", "")
        try:
            archive_weekly_report(start, end, note=note)
            flash("Weekly report archived.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.weekly_reports", date=base.isoformat()))

    archives = WeeklyReportArchive.query.order_by(WeeklyReportArchive.week_start.desc()).limit(20).all()
    return render_template(
        "weekly_reports.html",
        start=start,
        end=end,
        selected_date=base,
        student_hours=weekly_student_hours(start, end),
        therapist_hours=weekly_therapist_hours(start, end),
        admin_hours=admin_hours_summary(start, end),
        students={s.id: s for s in Student.query.all()},
        therapists={t.id: t for t in Therapist.query.all()},
        admins={a.id: a for a in AdminStaff.query.all()},
        archives=archives,
    )


@web_bp.route("/reports/weekly/archive/<int:archive_id>")
def weekly_archive_view(archive_id: int):
    archive = WeeklyReportArchive.query.get_or_404(archive_id)
    sections = get_archive_sections(archive)
    return render_template("weekly_archive_view.html", archive=archive, sections=sections)


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
        students=Student.query.order_by(Student.name).all(),
        admins=AdminStaff.query.filter_by(active=True).order_by(AdminStaff.name).all(),
        view=view,
        month=month,
        year=year,
        archived_groups=archived_groups,
    )


@web_bp.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])
def edit_payment(payment_id: int):
    payment = Payment.query.get_or_404(payment_id)

    if request.method == "POST":
        payment_date = request.form.get("payment_date")
        billing_period_start = request.form.get("billing_period_start")
        billing_period_end = request.form.get("billing_period_end")

        payment.payment_date = datetime.strptime(payment_date, "%Y-%m-%d").date() if payment_date else payment.payment_date
        payment.client_guardian_name = request.form.get("client_guardian_name", "")
        payment.student_id = int(request.form["student_id"])
        payment.purpose = request.form.get("purpose", "Therapy")
        payment.billing_period_start = datetime.strptime(billing_period_start, "%Y-%m-%d").date() if billing_period_start else None
        payment.billing_period_end = datetime.strptime(billing_period_end, "%Y-%m-%d").date() if billing_period_end else None
        payment.total_hours_rendered = float(request.form.get("total_hours_rendered", 0) or 0)
        payment.amount = float(request.form.get("amount", payment.amount) or 0)
        payment.received_by_admin_id = request.form.get("received_by_admin_id", type=int)
        payment.overpayment_amount = float(request.form.get("overpayment_amount", 0) or 0)
        payment.balance_after_payment = float(request.form.get("balance_after_payment", 0) or 0)
        payment.mode_of_transfer = request.form.get("mode_of_transfer", "Cash")
        payment.notes = request.form.get("notes", "")

        db.session.commit()
        flash(f"Payment entry {payment.id} updated.", "success")
        return redirect(url_for("web.payments_tracker"))

    return render_template(
        "payments_edit.html",
        payment=payment,
        students=Student.query.order_by(Student.name).all(),
        admins=AdminStaff.query.filter_by(active=True).order_by(AdminStaff.name).all(),
    )




@web_bp.route("/master-data")
def master_data_index():
    return render_template("master_data_index.html")


@web_bp.route("/master-data/students", methods=["GET", "POST"])
def master_students():
    if request.method == "POST":
        action = request.form.get("action", "create")
        name = request.form.get("name", "").strip()
        if not name:
            flash("Student name is required.", "error")
            return redirect(url_for("web.master_students"))

        if action == "create":
            student = Student(name=name)
            db.session.add(student)
        else:
            student = Student.query.get_or_404(int(request.form["student_id"]))
            student.name = name

        student.contract_hours_per_week = float(request.form.get("contract_hours_per_week", 0) or 0)
        student.overpayment_credit = float(request.form.get("overpayment_credit", 0) or 0)
        student.active = request.form.get("active", "1") == "1"
        db.session.commit()
        flash("Student saved.", "success")
        return redirect(url_for("web.master_students"))

    return render_template("master_students.html", students=Student.query.order_by(Student.name).all())


@web_bp.route("/master-data/therapists", methods=["GET", "POST"])
def master_therapists():
    if request.method == "POST":
        action = request.form.get("action", "create")
        name = request.form.get("name", "").strip()
        if not name:
            flash("Therapist name is required.", "error")
            return redirect(url_for("web.master_therapists"))
        if action == "create":
            therapist = Therapist(name=name)
            db.session.add(therapist)
        else:
            therapist = Therapist.query.get_or_404(int(request.form["therapist_id"]))
            therapist.name = name
        therapist.active = request.form.get("active", "1") == "1"
        db.session.commit()
        flash("Therapist saved.", "success")
        return redirect(url_for("web.master_therapists"))

    return render_template("master_therapists.html", therapists=Therapist.query.order_by(Therapist.name).all())


@web_bp.route("/master-data/admins", methods=["GET", "POST"])
def master_admins():
    if request.method == "POST":
        action = request.form.get("action", "create")
        name = request.form.get("name", "").strip()
        if not name:
            flash("Admin name is required.", "error")
            return redirect(url_for("web.master_admins"))
        if action == "create":
            admin = AdminStaff(name=name)
            db.session.add(admin)
        else:
            admin = AdminStaff.query.get_or_404(int(request.form["admin_id"]))
            admin.name = name
        admin.active = request.form.get("active", "1") == "1"
        db.session.commit()
        flash("Admin staff saved.", "success")
        return redirect(url_for("web.master_admins"))

    return render_template("master_admins.html", admins=AdminStaff.query.order_by(AdminStaff.name).all())


@web_bp.route("/master-data/schedules", methods=["GET", "POST"])
def master_schedules():
    if request.method == "POST":
        action = request.form.get("action", "create")
        student_id = request.form.get("student_id", type=int)
        therapist_id = request.form.get("therapist_id", type=int)
        day_of_week = request.form.get("day_of_week", type=int)
        start_raw = request.form.get("start_time", "")
        end_raw = request.form.get("end_time", "")
        if not all([student_id, therapist_id]) or day_of_week is None or not start_raw or not end_raw:
            flash("All schedule fields are required.", "error")
            return redirect(url_for("web.master_schedules"))

        if not Student.query.get(student_id) or not Therapist.query.get(therapist_id):
            flash("Invalid student or therapist reference.", "error")
            return redirect(url_for("web.master_schedules"))

        start_time_obj = datetime.strptime(start_raw, "%H:%M").time()
        end_time_obj = datetime.strptime(end_raw, "%H:%M").time()
        if end_time_obj <= start_time_obj:
            flash("End time must be after start time.", "error")
            return redirect(url_for("web.master_schedules"))

        duration_hours = (datetime.combine(date.today(), end_time_obj) - datetime.combine(date.today(), start_time_obj)).seconds / 3600

        conflict_query = RegularSchedule.query.filter_by(
            student_id=student_id,
            therapist_id=therapist_id,
            day_of_week=day_of_week,
            start_time=start_time_obj,
            end_time=end_time_obj,
        )

        if action == "create":
            if conflict_query.first():
                flash("Duplicate schedule row exists.", "error")
                return redirect(url_for("web.master_schedules"))
            sched = RegularSchedule(
                student_id=student_id,
                therapist_id=therapist_id,
                day_of_week=day_of_week,
                start_time=start_time_obj,
                end_time=end_time_obj,
                duration_hours=duration_hours,
                active=request.form.get("active", "1") == "1",
            )
            db.session.add(sched)
        else:
            sched = RegularSchedule.query.get_or_404(int(request.form["schedule_id"]))
            sched.student_id = student_id
            sched.therapist_id = therapist_id
            sched.day_of_week = day_of_week
            sched.start_time = start_time_obj
            sched.end_time = end_time_obj
            sched.duration_hours = duration_hours
            sched.active = request.form.get("active", "1") == "1"

        db.session.commit()
        flash("Regular schedule saved.", "success")
        return redirect(url_for("web.master_schedules"))

    return render_template(
        "master_schedules.html",
        schedules=RegularSchedule.query.order_by(RegularSchedule.day_of_week, RegularSchedule.start_time).all(),
        students=Student.query.order_by(Student.name).all(),
        therapists=Therapist.query.order_by(Therapist.name).all(),
        active_students=Student.query.filter_by(active=True).order_by(Student.name).all(),
        active_therapists=Therapist.query.filter_by(active=True).order_by(Therapist.name).all(),
    )




@web_bp.post("/master/students/<int:student_id>/deactivate")
def deactivate_student(student_id: int):
    student = Student.query.get_or_404(student_id)
    student.active = False
    db.session.commit()
    flash("Student deactivated.", "success")
    return redirect(url_for("web.master_students"))


@web_bp.post("/master/therapists/<int:therapist_id>/deactivate")
def deactivate_therapist(therapist_id: int):
    therapist = Therapist.query.get_or_404(therapist_id)
    therapist.active = False
    db.session.commit()
    flash("Therapist deactivated.", "success")
    return redirect(url_for("web.master_therapists"))


@web_bp.post("/master/admins/<int:admin_id>/deactivate")
def deactivate_admin(admin_id: int):
    admin = AdminStaff.query.get_or_404(admin_id)
    admin.active = False
    db.session.commit()
    flash("Admin deactivated.", "success")
    return redirect(url_for("web.master_admins"))


@web_bp.post("/master/schedules/<int:schedule_id>/deactivate")
def deactivate_schedule(schedule_id: int):
    sched = RegularSchedule.query.get_or_404(schedule_id)
    sched.active = False
    db.session.commit()
    flash("Schedule deactivated.", "success")
    return redirect(url_for("web.master_schedules"))


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
