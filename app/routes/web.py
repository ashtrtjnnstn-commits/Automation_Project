from __future__ import annotations

from datetime import date, datetime, time, timedelta
from calendar import monthrange
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, session, url_for

from app.models import (
    AdminAttendance,
    AdminStaff,
    AssessmentDepositLedger,
    AttendanceSession,
    BillingAdvice,
    BillingCycle,
    BillingLineItem,
    MonthlyPaymentArchive,
    Payment,
    PaymentAllocation,
    RedBillingNotice,
    RegularSchedule,
    RequiredDepositLedger,
    Student,
    Supervisor,
    Therapist,
    WeeklyReportArchive,
    db,
)
from app.services.admin_service import admin_hours_summary
from app.services.attendance_service import (
    missed_recovery_summary,
    preview_future_generated_sessions_sync,
    RENDERED_STATUSES,
    create_makeup_session,
    generate_monthly_sessions,
    get_month_sessions,
    sync_future_generated_sessions_for_schedule_change,
    update_session_status,
    weekly_student_hours,
    weekly_therapist_hours,
)
from app.services.billing_service import billing_hours_breakdown_for_advice, due_summary, generate_billing_advices_for_cycle, generate_billing_cycles_for_range
from app.services.import_export_service import (
    export_assessment_deposit_payment_history,
    export_admin_attendance,
    export_attendance_summary,
    export_billing_advices,
    export_payment_ledger,
    export_required_deposit_payment_history,
    export_therapist_weekly_hours,
    import_admin_staff,
    import_students_and_schedules,
    import_therapists,
)
from app.services.payment_service import (
    apply_payment_search,
    archive_payments,
    mark_archive_outdated_if_needed_for_payment_date,
    month_archive_summary,
    record_payment,
    refresh_month_archive_with_supervisor,
)
from app.services.weekly_archive_service import archive_weekly_report, get_archive_sections
from app.utils.audit_utils import log_audit
from app.utils.date_utils import week_bounds
from app.utils.backup_utils import (
    list_sqlite_backups,
    backup_sqlite_database,
    resolve_sqlite_db_path,
    restore_sqlite_backup,
    validate_backup_filename,
)

web_bp = Blueprint("web", __name__)

ATTENDANCE_STATUSES = ["Present", "Absent", "Cancelled", "Make-up", "Rescheduled", "No Show", "Non-billable", "Billed"]
BILLING_LOCKED_STATUSES = {"Issued", "Paid", "Archived"}
BILLING_EDITABLE_STATUSES = {"Draft", "Open"}
BILLING_ALLOWED_STATUSES = {"Draft", "Issued", "Paid", "Archived"}


def _is_billing_advice_editable(advice: BillingAdvice) -> bool:
    return advice.status in BILLING_EDITABLE_STATUSES


def _next_session_date(student_id: int, today: date) -> date | None:
    session = (
        AttendanceSession.query.filter(AttendanceSession.student_id == student_id, AttendanceSession.session_date > today)
        .order_by(AttendanceSession.session_date.asc())
        .first()
    )
    return session.session_date if session else None


def _red_bill_dates(student_id: int, today: date) -> tuple[date | None, date, bool]:
    next_session = _next_session_date(student_id, today)
    if not next_session:
        return None, today, True
    due_date = next_session - timedelta(days=1)
    if due_date < today:
        return next_session, today, True
    return next_session, due_date, False


def _is_advice_suspended(advice: BillingAdvice, today: date) -> bool:
    notice = RedBillingNotice.query.filter_by(billing_advice_id=advice.id).first()
    if not notice or advice.total_due <= 0:
        return False
    if notice.manual_lift_active:
        return False
    return notice.red_bill_due_date < today


def _student_is_suspended(student_id: int, on_date: date) -> bool:
    open_advices = (
        BillingAdvice.query.filter(
            BillingAdvice.student_id == student_id,
            BillingAdvice.total_due > 0,
            BillingAdvice.status.in_(["Draft", "Issued", "Open"]),
        ).all()
    )
    return any(_is_advice_suspended(advice, on_date) for advice in open_advices)


def _safe_float(raw_value: str | None, field_name: str) -> float:
    try:
        value = float(raw_value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number.")
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return value

@web_bp.route("/", methods=["GET", "POST"])
def dashboard():
    today = date.today()

    if request.method == "POST" and request.form.get("action") == "save_admin_attendance":
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
        return redirect(url_for("web.dashboard"))

    due = due_summary(today)

    billings_today = []
    for student in Student.query.filter_by(active=True).all():
        anchor = student.created_at.date()
        days = (today - anchor).days
        if days < 14:
            cycle_start = anchor
        else:
            cycle_start = anchor + timedelta(days=15 * (days // 15))
        cycle_end = cycle_start + timedelta(days=14)
        if cycle_end != today:
            continue

        cycle = BillingCycle.query.filter_by(start_date=cycle_start, end_date=cycle_end).first()
        existing = BillingAdvice.query.filter_by(student_id=student.id, billing_cycle_id=(cycle.id if cycle else None)).first() if cycle else None
        if not existing:
            billings_today.append({"student": student, "cycle_start": cycle_start, "cycle_end": cycle_end})

    upcoming_dues = BillingAdvice.query.join(BillingCycle).filter(
        BillingAdvice.total_due > 0,
        BillingAdvice.status.in_(["Draft", "Issued", "Open"]),
        BillingCycle.due_date > today,
        BillingCycle.due_date <= today + timedelta(days=3),
    ).all()
    overdue_dues = BillingAdvice.query.join(BillingCycle).filter(
        BillingAdvice.total_due > 0,
        BillingAdvice.status.in_(["Draft", "Issued", "Open"]),
        BillingCycle.due_date < today,
    ).all()

    red_bills_to_issue = []
    red_bills_active = []
    suspension_required = []
    for advice in overdue_dues:
        notice = RedBillingNotice.query.filter_by(billing_advice_id=advice.id).first()
        if not notice:
            next_session, red_due, immediate = _red_bill_dates(advice.student_id, today)
            red_bills_to_issue.append({"advice": advice, "next_session_date": next_session, "red_bill_due_date": red_due, "immediate": immediate})
            continue
        if advice.total_due <= 0:
            continue
        if notice.manual_lift_active:
            red_bills_active.append({"notice": notice, "advice": advice, "days_remaining": -1, "manual_override": True})
            continue
        if today <= notice.red_bill_due_date:
            days_remaining = (notice.red_bill_due_date - today).days
            red_bills_active.append({"notice": notice, "advice": advice, "days_remaining": days_remaining})
        else:
            suspension_required.append({"notice": notice, "advice": advice})

    recent_advices = BillingAdvice.query.order_by(BillingAdvice.created_at.desc()).limit(10).all()
    students = Student.query.order_by(Student.name).all()
    makeup_obligations = {s.id: missed_recovery_summary(student_id=s.id) for s in students}

    database_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    backups = []
    if database_uri.startswith("sqlite:///"):
        backups = list_sqlite_backups(database_uri)
        current_app.logger.info("Dashboard backup listing scanned uri=%s found=%d", database_uri, len(backups))

    month_last_day = monthrange(today.year, today.month)[1]
    if today.day == month_last_day:
        reminder_year, reminder_month = today.year, today.month
    else:
        prior_month_anchor = today.replace(day=1) - timedelta(days=1)
        reminder_year, reminder_month = prior_month_anchor.year, prior_month_anchor.month

    monthly_archive_done = (
        db.session.query(Payment.id)
        .filter(Payment.is_archived.is_(True), Payment.archive_year == reminder_year, Payment.archive_month == reminder_month)
        .first()
        is not None
    )

    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    reminder_week_end = last_sunday
    reminder_week_start = reminder_week_end - timedelta(days=6)
    weekly_archive_done = WeeklyReportArchive.query.filter_by(week_start=reminder_week_start, week_end=reminder_week_end).first() is not None

    last_month_archive = (
        db.session.query(Payment.archive_year, Payment.archive_month)
        .filter(Payment.is_archived.is_(True), Payment.archive_year.isnot(None), Payment.archive_month.isnot(None))
        .order_by(Payment.archive_year.desc(), Payment.archive_month.desc())
        .first()
    )
    last_week_archive = WeeklyReportArchive.query.order_by(WeeklyReportArchive.week_end.desc()).first()

    today_admin_count = AdminAttendance.query.filter(AdminAttendance.attendance_date == today).count()
    overdue_count = len(overdue_dues)
    pending_tasks_count = (
        (1 if overdue_count > 0 or len(billings_today) > 0 else 0)
        + (0 if monthly_archive_done else 1)
        + (0 if weekly_archive_done else 1)
    )

    admin_records = AdminAttendance.query.order_by(AdminAttendance.attendance_date.desc()).limit(10).all()

    return render_template(
        "dashboard.html",
        due=due,
        billings_today=billings_today,
        upcoming_dues=upcoming_dues,
        overdue_dues=overdue_dues,
        today=today,
        recent_advices=recent_advices,
        students=students,
        makeup_obligations=makeup_obligations,
        backups=backups,
        reminder_year=reminder_year,
        reminder_month=reminder_month,
        monthly_archive_done=monthly_archive_done,
        weekly_archive_done=weekly_archive_done,
        reminder_week_start=reminder_week_start,
        reminder_week_end=reminder_week_end,
        admins=AdminStaff.query.filter_by(active=True).order_by(AdminStaff.name).all(),
        admin_records=admin_records,
        overdue_count=overdue_count,
        pending_tasks_count=pending_tasks_count,
        today_admin_count=today_admin_count,
        last_month_archive=last_month_archive,
        last_week_archive=last_week_archive,
        red_bills_to_issue=red_bills_to_issue,
        red_bills_active=red_bills_active,
        suspension_required=suspension_required,
        supervisors=Supervisor.query.filter_by(is_active=True).order_by(Supervisor.name).all(),
    )


@web_bp.route("/restore-backup/<filename>", methods=["POST"])
def restore_backup(filename: str):
    selected_backup = None
    db_path = None
    database_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")

    try:
        validate_backup_filename(filename)
        db_path = resolve_sqlite_db_path(database_uri)
        backup_dir = db_path.parent / "backups"
        selected_backup = (backup_dir / filename).resolve()

        if selected_backup.parent != backup_dir.resolve():
            raise ValueError("Invalid backup filename.")
        _, emergency_backup = restore_sqlite_backup(database_uri, selected_backup)
        flash(
            f"Backup restored from {filename}. Emergency pre-restore backup: "
            f"{emergency_backup.name if emergency_backup else 'not created'}.",
            "success",
        )
        log_audit(
            "backup_restore_attempt",
            "Database",
            None,
            f"result=success|source={filename}|target={db_path}",
        )
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        log_audit(
            "backup_restore_attempt",
            "Database",
            None,
            f"result=failed|source={filename}|target={db_path}|error={exc}",
        )
        flash(str(exc), "error")
    except OSError:
        log_audit(
            "backup_restore_attempt",
            "Database",
            None,
            f"result=failed|source={filename}|target={db_path}|error=filesystem",
        )
        flash("Restore failed due to filesystem error.", "error")
    except Exception:
        log_audit(
            "backup_restore_attempt",
            "Database",
            None,
            f"result=failed|source={filename}|target={db_path}|error=unexpected",
        )
        flash("Restore failed due to an unexpected error.", "error")

    return redirect(url_for("web.dashboard"))


@web_bp.route("/create-backup", methods=["POST"])
def create_backup():
    database_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not database_uri.startswith("sqlite:///"):
        flash("Backup is only supported for sqlite file databases.", "error")
        return redirect(url_for("web.dashboard"))

    backup_path = backup_sqlite_database(database_uri)
    if not backup_path:
        flash("Backup creation failed. Ensure the sqlite database file exists.", "error")
        return redirect(url_for("web.dashboard"))

    flash(f"Backup created: {backup_path.name}", "success")
    return redirect(url_for("web.dashboard"))


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
    selected_raw = request.form.get("selected_date") if request.method == "POST" else request.args.get("date")
    selected = datetime.strptime(selected_raw or date.today().isoformat(), "%Y-%m-%d").date()
    sessions = AttendanceSession.query.filter_by(session_date=selected).order_by(AttendanceSession.start_time).all()
    suspended_student_ids = {s.student_id for s in sessions if _student_is_suspended(s.student_id, selected)}

    if request.method == "POST":
        session_ids = request.form.getlist("session_ids")
        updated = 0
        for raw_id in session_ids:
            status = request.form.get(f"status_{raw_id}", "")
            session = AttendanceSession.query.get(int(raw_id))
            if not session:
                continue
            if session.student_id in suspended_student_ids:
                if session.status != "Suspended":
                    update_session_status(session.id, "Suspended")
                    updated += 1
                continue
            if session.status != status:
                update_session_status(session.id, status)
                updated += 1
        flash(f"Updated {updated} session(s).", "success")
        return redirect(url_for("web.daily_schedule", date=request.form["selected_date"]))

    return render_template(
        "daily_schedule.html",
        selected=selected,
        sessions=sessions,
        attendance_statuses=ATTENDANCE_STATUSES,
        suspended_student_ids=suspended_student_ids,
    )


@web_bp.route("/students/<int:student_id>")
def student_profile(student_id: int):
    student = Student.query.get_or_404(student_id)
    sessions = AttendanceSession.query.filter_by(student_id=student.id).order_by(AttendanceSession.session_date.desc()).limit(20).all()
    open_advices = (
        BillingAdvice.query.filter(BillingAdvice.student_id == student.id, BillingAdvice.status.in_(["Draft", "Issued", "Open"]))
        .all()
    )
    missed_summary = missed_recovery_summary(student_id=student.id)
    return render_template("student_profile.html", student=student, sessions=sessions, open_advices=open_advices, missed_summary=missed_summary)


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
    return reports_page()


@web_bp.route("/reports", methods=["GET", "POST"])
def reports_page():
    base = datetime.strptime(request.values.get("date", date.today().isoformat()), "%Y-%m-%d").date()
    start, end = week_bounds(base)

    if request.method == "POST" and request.form.get("action") == "archive_week":
        note = request.form.get("note", "")
        try:
            archive_weekly_report(start, end, note=note)
            flash(f"Weekly report for {start.isoformat()} to {end.isoformat()} archived successfully.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.weekly_reports", date=base.isoformat()))

    archives = WeeklyReportArchive.query.order_by(WeeklyReportArchive.week_start.desc()).limit(20).all()
    students = Student.query.all()
    student_hours = weekly_student_hours(start, end)
    return render_template(
        "reports.html",
        start=start,
        end=end,
        selected_date=base,
        student_hours=student_hours,
        therapist_hours=weekly_therapist_hours(start, end),
        admin_hours=admin_hours_summary(start, end),
        students={s.id: s for s in students},
        therapists={t.id: t for t in Therapist.query.all()},
        admins={a.id: a for a in AdminStaff.query.all()},
        archives=archives,
        export_students=Student.query.filter_by(active=True).order_by(Student.name).all(),
    )


@web_bp.route("/reports/weekly/archive/<int:archive_id>")
def weekly_archive_view(archive_id: int):
    archive = WeeklyReportArchive.query.get_or_404(archive_id)
    sections = get_archive_sections(archive)
    return render_template("weekly_archive_view.html", archive=archive, sections=sections)


@web_bp.route("/reports/export")
def export_reports_page():
    return redirect(url_for("web.reports_page"))


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
        return redirect(url_for("web.dashboard"))
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

    advice_hours = {a.id: billing_hours_breakdown_for_advice(a) for a in generated_advices}
    recent_advice_hours = {a.id: billing_hours_breakdown_for_advice(a) for a in student_advices}

    return render_template(
        "billing.html",
        cycles=cycles,
        students=Student.query.filter_by(active=True).all(),
        selected_student_id=selected_student_id,
        selected_student=selected_student,
        generated_advices=generated_advices,
        student_advices=student_advices,
        advice_hours=advice_hours,
        recent_advice_hours=recent_advice_hours,
    )






@web_bp.post("/billing/<int:advice_id>/issue-red-bill")
def issue_red_bill(advice_id: int):
    advice = BillingAdvice.query.get_or_404(advice_id)
    if advice.total_due <= 0:
        flash("Billing advice is already settled.", "error")
        return redirect(url_for("web.dashboard"))

    today = date.today()
    next_session, red_due, immediate = _red_bill_dates(advice.student_id, today)
    notice = RedBillingNotice.query.filter_by(billing_advice_id=advice.id).first()
    if notice:
        flash("Red Bill already issued for this billing advice.", "error")
        return redirect(url_for("web.dashboard"))

    notice = RedBillingNotice(billing_advice_id=advice.id, student_id=advice.student_id, issued_date=today)
    db.session.add(notice)
    notice.outstanding_amount = advice.total_due
    notice.next_session_date = next_session
    notice.red_bill_due_date = red_due
    notice.status = "issued"
    db.session.commit()
    log_audit("red_bill_issued", "BillingAdvice", advice.id, f"due={red_due.isoformat()}|immediate={immediate}")
    flash(f"Red Bill issued for {advice.student.name}. Red Bill due date: {red_due}.", "success")
    return redirect(url_for("web.dashboard"))


@web_bp.post("/billing/<int:advice_id>/suspension-exception")
def approve_suspension_exception(advice_id: int):
    advice = BillingAdvice.query.get_or_404(advice_id)
    notice = RedBillingNotice.query.filter_by(billing_advice_id=advice.id).first()
    if not notice or advice.total_due <= 0:
        flash("No active suspension case for this billing advice.", "error")
        return redirect(url_for("web.dashboard"))

    supervisor = Supervisor.query.filter_by(name=request.form.get("supervisor_name", ""), is_active=True).first()
    from werkzeug.security import check_password_hash

    if not supervisor or not check_password_hash(supervisor.password_hash, request.form.get("supervisor_password", "")):
        flash("Supervisor approval failed. Please check credentials and active status.", "error")
        return redirect(url_for("web.dashboard"))

    notice.manual_lift_active = True
    notice.manual_lift_reason = request.form.get("override_reason", "").strip()
    notice.manual_lift_by_supervisor_id = supervisor.id
    notice.manual_lift_at = datetime.utcnow()
    db.session.commit()
    log_audit(
        "suspension_partial_settlement_lift_approved",
        "BillingAdvice",
        advice.id,
        f"approver={supervisor.name}|reason={notice.manual_lift_reason}",
    )
    flash(f"Suspension exception approved for {advice.student.name}. Attendance is temporarily unlocked.", "success")
    return redirect(url_for("web.dashboard"))


@web_bp.post("/billing/<int:advice_id>/approve-frozen-edit")
def approve_frozen_billing_edit(advice_id: int):
    advice = BillingAdvice.query.get_or_404(advice_id)
    supervisor = Supervisor.query.filter_by(name=request.form.get("supervisor_name", ""), is_active=True).first()
    from werkzeug.security import check_password_hash

    if not supervisor or not supervisor.can_approve_archive_refresh or not check_password_hash(supervisor.password_hash, request.form.get("supervisor_password", "")):
        flash("Supervisor approval failed. Please check credentials and active status.", "error")
        return redirect(url_for("web.billing_edit", advice_id=advice_id))

    session[f"frozen_edit_{advice_id}"] = True
    reason = request.form.get("approval_reason", "")
    log_audit("frozen_billing_correction_approved", "BillingAdvice", advice.id, f"approver={supervisor.name}|role={supervisor.role}|reason={reason}")
    flash("Frozen billing correction approved. You may now edit this advice.", "success")
    return redirect(url_for("web.billing_edit", advice_id=advice_id, approved=1))


@web_bp.route("/billing/<int:advice_id>/edit", methods=["GET", "POST"])
def billing_edit(advice_id: int):
    advice = BillingAdvice.query.get_or_404(advice_id)
    cycle = advice.billing_cycle

    is_locked = not _is_billing_advice_editable(advice)
    is_approved = session.get(f"frozen_edit_{advice.id}") or request.args.get("approved") == "1"
    if is_locked and not is_approved:
        return render_template("billing_frozen_approval.html", advice=advice, supervisors=Supervisor.query.filter_by(is_active=True).order_by(Supervisor.name).all())

    if request.method == "POST":
        cycle.start_date = datetime.strptime(request.form["cycle_start"], "%Y-%m-%d").date()
        cycle.end_date = datetime.strptime(request.form["cycle_end"], "%Y-%m-%d").date()
        cycle.issue_date = datetime.strptime(request.form["issue_date"], "%Y-%m-%d").date()
        cycle.due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()

        advice.subtotal_sessions = float(request.form.get("session_subtotal", advice.subtotal_sessions) or 0)
        advice.required_deposit_charge = float(request.form.get("required_deposit", advice.required_deposit_charge) or 0)
        advice.assessment_deposit_charge = float(request.form.get("assessment_deposit", advice.assessment_deposit_charge) or 0)
        advice.old_balance = float(request.form.get("old_balance", advice.old_balance) or 0)
        advice.overpayment_credit = float(request.form.get("credit", advice.overpayment_credit) or 0)
        advice.total_due = float(request.form.get("total_due", advice.total_due) or 0)
        form_status = request.form.get("status", advice.status)
        if form_status in BILLING_ALLOWED_STATUSES:
            advice.status = form_status

        remarks = request.form.get("remarks", "")
        notes_item = BillingLineItem.query.filter_by(billing_advice_id=advice.id, item_type="billing_remarks").first()
        if notes_item:
            notes_item.description = remarks
            notes_item.quantity = 0
            notes_item.rate = 0
            notes_item.amount = 0
        elif remarks:
            db.session.add(BillingLineItem(billing_advice_id=advice.id, item_type="billing_remarks", description=remarks, quantity=0, rate=0, amount=0))

        overrides = {
            "regular_rendered_hours": request.form.get("regular_rendered_hours", type=float),
            "makeup_rendered_hours": request.form.get("makeup_rendered_hours", type=float),
            "billed_hours": request.form.get("billed_hours", type=float),
            "total_rendered_hours": request.form.get("total_rendered_hours", type=float),
            "billable_hours": request.form.get("billable_hours", type=float),
            "non_billable_hours": request.form.get("non_billable_hours", type=float),
        }
        for item_type, value in overrides.items():
            existing = BillingLineItem.query.filter_by(billing_advice_id=advice.id, item_type=item_type).first()
            if existing:
                existing.quantity = float(value or 0)
                existing.description = item_type.replace("_", " ").title()
                existing.rate = 0
                existing.amount = 0
            elif value is not None:
                db.session.add(BillingLineItem(billing_advice_id=advice.id, item_type=item_type, description=item_type.replace("_", " ").title(), quantity=float(value or 0), rate=0, amount=0))

        db.session.commit()
        if is_locked:
            session.pop(f"frozen_edit_{advice.id}", None)
            log_audit("frozen_billing_correction_applied", "BillingAdvice", advice.id, "Approved frozen billing correction applied")
        log_audit("billing_advice_edited", "BillingAdvice", advice.id, "Manual advice edit")
        flash("Billing advice updated.", "success")
        return redirect(url_for("web.billing_page", student_id=advice.student_id))

    hours = billing_hours_breakdown_for_advice(advice)
    remarks_item = BillingLineItem.query.filter_by(billing_advice_id=advice.id, item_type="billing_remarks").first()
    return render_template("billing_edit.html", advice=advice, cycle=cycle, hours=hours, remarks=(remarks_item.description if remarks_item else ""))


@web_bp.post("/billing/<int:advice_id>/status")
def billing_mark_status(advice_id: int):
    advice = BillingAdvice.query.get_or_404(advice_id)
    new_status = request.form.get("status", "").strip()
    if new_status not in BILLING_ALLOWED_STATUSES:
        flash("Invalid billing advice status.", "error")
        return redirect(url_for("web.billing_page", student_id=advice.student_id))

    old_status = advice.status
    advice.status = new_status
    db.session.commit()
    log_audit(f"billing_advice_marked_{new_status.lower()}", "BillingAdvice", advice.id, f"{old_status}->{new_status}")
    flash(f"Billing advice marked as {new_status}.", "success")
    return redirect(url_for("web.billing_page", student_id=advice.student_id))


@web_bp.post("/billing/<int:advice_id>/delete")
def billing_delete(advice_id: int):
    advice = BillingAdvice.query.get_or_404(advice_id)
    if not _is_billing_advice_editable(advice):
        flash("Billing advice is locked and cannot be deleted.", "error")
        return redirect(url_for("web.billing_page", student_id=advice.student_id))
    if advice.payment_allocations:
        flash("Cannot delete billing advice with linked payment allocations.", "error")
        return redirect(url_for("web.billing_page", student_id=advice.student_id))

    aid = advice.id
    BillingLineItem.query.filter_by(billing_advice_id=advice.id).delete()
    RequiredDepositLedger.query.filter_by(billing_advice_id=advice.id).delete()
    AssessmentDepositLedger.query.filter_by(billing_advice_id=advice.id).delete()
    db.session.delete(advice)
    db.session.commit()
    log_audit("billing_advice_deleted", "BillingAdvice", aid, "Advice deleted")
    flash("Billing advice deleted.", "success")
    return redirect(url_for("web.billing_page", student_id=advice.student_id))


def _build_payment_history_context(view: str, month: int, year: int, search_query: str) -> dict:
    archived_groups = (
        db.session.query(Payment.archive_year, Payment.archive_month)
        .filter(Payment.is_archived.is_(True))
        .distinct()
        .order_by(Payment.archive_year.desc(), Payment.archive_month.desc())
        .all()
    )
    archive_status_map = {
        (a.archive_year, a.archive_month): a.status
        for a in MonthlyPaymentArchive.query.filter(MonthlyPaymentArchive.archive_year.isnot(None)).all()
    }

    if view == "archived" and archived_groups and (year, month) not in archived_groups:
        year, month = archived_groups[0]

    query = Payment.query.join(Student)
    if view == "archived":
        query = query.filter(Payment.is_archived.is_(True), Payment.archive_month == month, Payment.archive_year == year)
    else:
        query = query.filter(Payment.is_archived.is_(False))
        query = query.filter(
            Payment.payment_date >= date(year, month, 1),
            Payment.payment_date < (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)),
        )

    query = apply_payment_search(query, search_query)
    payments = query.order_by(Payment.payment_date.desc(), Payment.id.desc()).all()

    return {
        "payments": payments,
        "view": view,
        "month": month,
        "year": year,
        "archived_groups": archived_groups,
        "search_query": search_query,
        "payments_count": len(payments),
        "archive_status_map": archive_status_map,
    }


@web_bp.route("/payments", methods=["GET", "POST"])
def payments_page():
    if request.method == "POST" and request.form.get("action") == "archive":
        month = int(request.form["archive_month"])
        year = int(request.form["archive_year"])
        try:
            count = archive_payments(month=month, year=year)
            label = date(year, month, 1).strftime("%B %Y")
            flash(f"{label} ledger archived successfully ({count} record(s)).", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("web.payments_page", view="archived", month=month, year=year))

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
        if mark_archive_outdated_if_needed_for_payment_date(payment.payment_date):
            month_label = payment.payment_date.strftime("%B %Y")
            flash(
                f"This payment affects {month_label}, which is already archived. The archive has been marked Outdated and requires supervisor approval to refresh.",
                "error",
            )
        return redirect(url_for("web.payments_page"))

    today = date.today()
    view = request.args.get("view", "active")
    month = request.args.get("month", type=int) or today.month
    year = request.args.get("year", type=int) or today.year
    search_query = request.args.get("q", "").strip()
    history_ctx = _build_payment_history_context(view=view, month=month, year=year, search_query=search_query)

    return render_template(
        "payments.html",
        students=Student.query.order_by(Student.name).all(),
        admins=AdminStaff.query.filter_by(active=True).order_by(AdminStaff.name).all(),
        **history_ctx,
    )


@web_bp.route("/payments/tracker", methods=["GET", "POST"])
def payments_tracker():
    return payments_page()


@web_bp.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])
def edit_payment(payment_id: int):
    payment = Payment.query.get_or_404(payment_id)

    if request.method == "POST":
        old_payment_date = payment.payment_date
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
        if mark_archive_outdated_if_needed_for_payment_date(old_payment_date) or mark_archive_outdated_if_needed_for_payment_date(payment.payment_date):
            month_label = payment.payment_date.strftime("%B %Y")
            flash(
                f"This payment affects {month_label}, which is already archived. The archive has been marked Outdated and requires supervisor approval to refresh.",
                "error",
            )
        log_audit("payment_edited", "Payment", payment.id, "Payment edited from web form")
        flash(f"Payment entry {payment.id} updated.", "success")
        return redirect(url_for("web.payments_tracker"))

    return render_template(
        "payments_edit.html",
        payment=payment,
        students=Student.query.order_by(Student.name).all(),
        admins=AdminStaff.query.filter_by(active=True).order_by(AdminStaff.name).all(),
    )


@web_bp.post("/payments/<int:payment_id>/delete")
def delete_payment(payment_id: int):
    payment = Payment.query.get_or_404(payment_id)
    impacted_date = payment.payment_date
    db.session.delete(payment)
    db.session.commit()
    if mark_archive_outdated_if_needed_for_payment_date(impacted_date):
        month_label = impacted_date.strftime("%B %Y")
        flash(
            f"This payment affects {month_label}, which is already archived. The archive has been marked Outdated and requires supervisor approval to refresh.",
            "error",
        )
    flash(f"Payment entry {payment_id} deleted.", "success")
    return redirect(url_for("web.payments_tracker"))


@web_bp.route("/payments/archive-review/<int:year>/<int:month>", methods=["GET", "POST"])
def payment_archive_review(year: int, month: int):
    snapshot = MonthlyPaymentArchive.query.filter_by(archive_year=year, archive_month=month).first_or_404()
    summary = month_archive_summary(month=month, year=year)
    period_start = date(year, month, 1)
    period_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    live_entries = (
        Payment.query.filter(Payment.payment_date >= period_start, Payment.payment_date < period_end)
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
        .all()
    )

    if request.method == "POST":
        ok, message = refresh_month_archive_with_supervisor(
            month=month,
            year=year,
            supervisor_name=request.form.get("supervisor_name", ""),
            password=request.form.get("supervisor_password", ""),
            reason=request.form.get("refresh_reason", "").strip(),
        )
        if ok:
            flash(message, "success")
            return redirect(url_for("web.payments_tracker", view="archived", month=month, year=year))
        flash(message, "error")

    supervisors = Supervisor.query.filter_by(is_active=True).order_by(Supervisor.name).all()
    return render_template(
        "payment_archive_review.html",
        year=year,
        month=month,
        snapshot=snapshot,
        summary=summary,
        live_entries=live_entries,
        supervisors=supervisors,
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

        try:
            contract_hours_per_week = _safe_float(request.form.get("contract_hours_per_week"), "Contract Hours/Week")
            overpayment_credit = _safe_float(request.form.get("overpayment_credit"), "Overpayment Credit")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("web.master_students"))

        if action == "create":
            student = Student(name=name)
            db.session.add(student)
            old_required_policy = student.required_deposit_enabled
            old_assessment_policy = student.assessment_deposit_enabled
        else:
            student = Student.query.get_or_404(int(request.form["student_id"]))
            student.name = name
            old_required_policy = student.required_deposit_enabled
            old_assessment_policy = student.assessment_deposit_enabled

        student.contract_hours_per_week = contract_hours_per_week
        student.overpayment_credit = overpayment_credit

        student.required_deposit_enabled = request.form.get("required_deposit_enabled", "1") == "1"
        student.assessment_deposit_enabled = request.form.get("assessment_deposit_enabled", "1") == "1"
        if action == "create" and not student.required_deposit_enabled:
            student.required_deposit_total = 0.0
            student.required_deposit_billed = 0.0
            student.required_deposit_paid = 0.0
        if student.assessment_deposit_enabled and (student.assessment_deposit_total or 0) <= 0:
            student.assessment_deposit_total = 5000.0
        if action == "create" and not student.assessment_deposit_enabled:
            student.assessment_deposit_total = 0.0
            student.assessment_deposit_billed = 0.0
            student.assessment_deposit_paid = 0.0
        student.active = request.form.get("active", "1") == "1"
        db.session.commit()

        if old_required_policy != student.required_deposit_enabled:
            log_audit(
                "student_required_deposit_policy_changed",
                "Student",
                student.id,
                f"student={student.name}|old={old_required_policy}|new={student.required_deposit_enabled}",
            )
        if old_assessment_policy != student.assessment_deposit_enabled:
            log_audit(
                "student_assessment_deposit_policy_changed",
                "Student",
                student.id,
                f"student={student.name}|old={old_assessment_policy}|new={student.assessment_deposit_enabled}",
            )
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




@web_bp.route("/master-data/supervisors", methods=["GET", "POST"])
def master_supervisors():
    if request.method == "POST":
        action = request.form.get("action", "create")
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "").strip()
        if not name or not role:
            flash("Supervisor name and role are required.", "error")
            return redirect(url_for("web.master_supervisors"))

        if action == "create":
            from werkzeug.security import generate_password_hash
            password = request.form.get("password", "").strip()
            if not password:
                flash("Password is required for new supervisors.", "error")
                return redirect(url_for("web.master_supervisors"))
            row = Supervisor(name=name, role=role, password_hash=generate_password_hash(password), is_active=True)
            db.session.add(row)
        else:
            from werkzeug.security import generate_password_hash
            row = Supervisor.query.get_or_404(int(request.form["supervisor_id"]))
            row.name = name
            row.role = role
            row.is_active = request.form.get("is_active", "1") == "1"
            password = request.form.get("password", "").strip()
            if password:
                row.password_hash = generate_password_hash(password)

        db.session.commit()
        flash("Supervisor saved.", "success")
        return redirect(url_for("web.master_supervisors"))

    return render_template("master_supervisors.html", supervisors=Supervisor.query.order_by(Supervisor.name).all())


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
        if request.form.get("sync_cancel") == "1":
            flash("Schedule sync preview canceled. No attendance changes were applied.", "success")
            return redirect(url_for("web.master_schedules"))

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
            old_day_of_week = sched.day_of_week
            old_start_time = sched.start_time
            old_therapist_id = sched.therapist_id
            apply_future_sync = request.form.get("apply_future_sync", "1") == "1"
            effective_from_raw = request.form.get("effective_from", "")
            sync_confirmed = request.form.get("sync_confirmed", "0") == "1"
            if apply_future_sync and not sync_confirmed:
                try:
                    effective_from = datetime.strptime(effective_from_raw, "%Y-%m-%d").date() if effective_from_raw else date.today()
                except ValueError:
                    flash("Effective date is invalid.", "error")
                    return redirect(url_for("web.master_schedules"))

                preview = preview_future_generated_sessions_sync(
                    schedule=sched,
                    proposed_day_of_week=day_of_week,
                    proposed_start_time=start_time_obj,
                    proposed_therapist_id=therapist_id,
                    proposed_duration_hours=duration_hours,
                    proposed_end_time=end_time_obj,
                    proposed_active=request.form.get("active", "1") == "1",
                    previous_day_of_week=old_day_of_week,
                    previous_start_time=old_start_time,
                    previous_therapist_id=old_therapist_id,
                    effective_from=effective_from,
                )
                return render_template(
                    "master_schedules.html",
                    schedules=RegularSchedule.query.order_by(RegularSchedule.day_of_week, RegularSchedule.start_time).all(),
                    students=Student.query.order_by(Student.name).all(),
                    therapists=Therapist.query.order_by(Therapist.name).all(),
                    active_students=Student.query.filter_by(active=True).order_by(Student.name).all(),
                    active_therapists=Therapist.query.filter_by(active=True).order_by(Therapist.name).all(),
                    sync_preview={
                        "schedule_id": sched.id,
                        "student_id": student_id,
                        "therapist_id": therapist_id,
                        "day_of_week": day_of_week,
                        "start_time": start_raw,
                        "end_time": end_raw,
                        "active": request.form.get("active", "1"),
                        "effective_from": effective_from.isoformat(),
                        "counts": preview,
                        "is_past_or_today": effective_from <= date.today(),
                    },
                )

            sched.student_id = student_id
            sched.therapist_id = therapist_id
            sched.day_of_week = day_of_week
            sched.start_time = start_time_obj
            sched.end_time = end_time_obj
            sched.duration_hours = duration_hours
            sched.active = request.form.get("active", "1") == "1"

            if apply_future_sync:
                effective_from = datetime.strptime(effective_from_raw, "%Y-%m-%d").date() if effective_from_raw else date.today()
                removed, added = sync_future_generated_sessions_for_schedule_change(
                    schedule=sched,
                    previous_day_of_week=old_day_of_week,
                    previous_start_time=old_start_time,
                    previous_therapist_id=old_therapist_id,
                    effective_from=effective_from,
                )
                flash("Schedule updated successfully.", "success")
                flash(f"{removed} outdated future sessions removed.", "success")
                flash(f"{added} new future sessions added.", "success")
                flash("Recorded attendance and overrides were preserved.", "success")

        db.session.commit()
        if action == "create" or request.form.get("apply_future_sync", "1") != "1":
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
    student_id = request.args.get("student_id", type=int)
    try:
        if kind == "attendance":
            path = export_attendance_summary("exports/attendance.xlsx")
        elif kind == "admin":
            path = export_admin_attendance("exports/admin_attendance.xlsx")
        elif kind == "billing":
            path = export_billing_advices("exports/billing_advices.xlsx")
        elif kind == "payments":
            path = export_payment_ledger("exports/payment_ledger.xlsx")
        elif kind == "required-deposit-history":
            suffix = f"_student_{student_id}" if student_id else "_all"
            path = export_required_deposit_payment_history(f"exports/required_deposit_history{suffix}.xlsx", student_id=student_id)
        elif kind == "assessment-deposit-history":
            suffix = f"_student_{student_id}" if student_id else "_all"
            path = export_assessment_deposit_payment_history(f"exports/assessment_deposit_history{suffix}.xlsx", student_id=student_id)
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
