from __future__ import annotations

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint


db = SQLAlchemy()


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Therapist(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)


class AdminStaff(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)


class Student(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    assigned_therapist_id = db.Column(db.Integer, db.ForeignKey("therapist.id"), nullable=True)
    contract_hours_per_week = db.Column(db.Float, default=0.0, nullable=False)
    has_weekday_rate = db.Column(db.Boolean, default=True, nullable=False)
    has_weekend_rate = db.Column(db.Boolean, default=False, nullable=False)
    required_deposit_enabled = db.Column(db.Boolean, default=True, nullable=False)
    required_deposit_total = db.Column(db.Float, default=0.0, nullable=False)
    required_deposit_billed = db.Column(db.Float, default=0.0, nullable=False)
    required_deposit_paid = db.Column(db.Float, default=0.0, nullable=False)
    assessment_deposit_total = db.Column(db.Float, default=5000.0, nullable=False)
    assessment_deposit_billed = db.Column(db.Float, default=0.0, nullable=False)
    assessment_deposit_paid = db.Column(db.Float, default=0.0, nullable=False)
    overpayment_credit = db.Column(db.Float, default=0.0, nullable=False)
    notes = db.Column(db.Text, default="")

    assigned_therapist = db.relationship("Therapist", backref="students")


class RegularSchedule(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    therapist_id = db.Column(db.Integer, db.ForeignKey("therapist.id"), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Mon
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=True)
    duration_hours = db.Column(db.Float, nullable=False)
    effective_from = db.Column(db.Date, nullable=True)
    effective_to = db.Column(db.Date, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)

    student = db.relationship("Student", backref="regular_schedules")
    therapist = db.relationship("Therapist", backref="regular_schedules")


class AttendanceSession(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    therapist_id = db.Column(db.Integer, db.ForeignKey("therapist.id"), nullable=False)
    session_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    duration_hours = db.Column(db.Float, nullable=False)
    session_type = db.Column(db.String(30), default="regular", nullable=False)
    source_type = db.Column(db.String(30), default="generated", nullable=False)
    linked_regular_schedule_id = db.Column(db.Integer, db.ForeignKey("regular_schedule.id"), nullable=True)
    status = db.Column(db.String(30), default="", nullable=False)
    notes = db.Column(db.Text, default="")
    billing_included = db.Column(db.Boolean, default=False, nullable=False)
    payroll_included = db.Column(db.Boolean, default=False, nullable=False)
    is_billable_override = db.Column(db.Boolean, default=False, nullable=False)

    student = db.relationship("Student", backref="attendance_sessions")
    therapist = db.relationship("Therapist", backref="attendance_sessions")
    linked_regular_schedule = db.relationship("RegularSchedule", backref="generated_sessions")

    __table_args__ = (
        UniqueConstraint("student_id", "session_date", "start_time", "source_type", name="uniq_session_slot_source"),
    )


class SessionOverride(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_session_id = db.Column(db.Integer, db.ForeignKey("attendance_session.id"), nullable=True)
    new_session_id = db.Column(db.Integer, db.ForeignKey("attendance_session.id"), nullable=False)
    override_type = db.Column(db.String(30), nullable=False)  # makeup/rescheduled/replacement
    reason = db.Column(db.Text, default="")


class AdminAttendance(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admin_staff.id"), nullable=False)
    attendance_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(30), default="Present", nullable=False)
    shift_label = db.Column(db.String(50), default="")
    hours_worked = db.Column(db.Float, default=0.0, nullable=False)
    notes = db.Column(db.Text, default="")

    admin = db.relationship("AdminStaff", backref="attendance_records")


class BillingCycle(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default="Open", nullable=False)

    __table_args__ = (UniqueConstraint("start_date", "end_date", name="uniq_billing_cycle"),)


class BillingAdvice(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    billing_cycle_id = db.Column(db.Integer, db.ForeignKey("billing_cycle.id"), nullable=False)
    subtotal_sessions = db.Column(db.Float, default=0.0, nullable=False)
    old_balance = db.Column(db.Float, default=0.0, nullable=False)
    overpayment_credit = db.Column(db.Float, default=0.0, nullable=False)
    required_deposit_charge = db.Column(db.Float, default=0.0, nullable=False)
    assessment_deposit_charge = db.Column(db.Float, default=0.0, nullable=False)
    total_due = db.Column(db.Float, default=0.0, nullable=False)
    status = db.Column(db.String(20), default="Draft", nullable=False)

    student = db.relationship("Student", backref="billing_advices")
    billing_cycle = db.relationship("BillingCycle", backref="billing_advices")

    __table_args__ = (UniqueConstraint("student_id", "billing_cycle_id", name="uniq_student_cycle_advice"),)


class BillingLineItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    billing_advice_id = db.Column(db.Integer, db.ForeignKey("billing_advice.id"), nullable=False)
    item_type = db.Column(db.String(40), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Float, default=0.0, nullable=False)
    rate = db.Column(db.Float, default=0.0, nullable=False)
    amount = db.Column(db.Float, default=0.0, nullable=False)

    billing_advice = db.relationship("BillingAdvice", backref="line_items")


class Payment(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    client_guardian_name = db.Column(db.String(120), default="", nullable=False)
    purpose = db.Column(db.String(40), default="Therapy", nullable=False)
    billing_period_start = db.Column(db.Date, nullable=True)
    billing_period_end = db.Column(db.Date, nullable=True)
    total_hours_rendered = db.Column(db.Float, default=0.0, nullable=False)
    received_by_admin_id = db.Column(db.Integer, db.ForeignKey("admin_staff.id"), nullable=True)
    overpayment_amount = db.Column(db.Float, default=0.0, nullable=False)
    balance_after_payment = db.Column(db.Float, default=0.0, nullable=False)
    mode_of_transfer = db.Column(db.String(40), default="Cash", nullable=False)
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    archive_year = db.Column(db.Integer, nullable=True)
    archive_month = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.Text, default="")

    student = db.relationship("Student", backref="payments")
    received_by_admin = db.relationship("AdminStaff", backref="received_payments")


class PaymentAllocation(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payment.id"), nullable=False)
    billing_advice_id = db.Column(db.Integer, db.ForeignKey("billing_advice.id"), nullable=True)
    allocation_type = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Float, nullable=False)

    payment = db.relationship("Payment", backref="allocations")
    billing_advice = db.relationship("BillingAdvice", backref="payment_allocations")


class RequiredDepositLedger(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    billing_cycle_id = db.Column(db.Integer, db.ForeignKey("billing_cycle.id"), nullable=True)
    billing_advice_id = db.Column(db.Integer, db.ForeignKey("billing_advice.id"), nullable=True)
    entry_type = db.Column(db.String(20), nullable=False)  # billed/paid/adjustment
    amount = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, default="")


class AssessmentDepositLedger(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    billing_cycle_id = db.Column(db.Integer, db.ForeignKey("billing_cycle.id"), nullable=True)
    billing_advice_id = db.Column(db.Integer, db.ForeignKey("billing_advice.id"), nullable=True)
    entry_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, default="")


class TherapistPayrollSummary(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    therapist_id = db.Column(db.Integer, db.ForeignKey("therapist.id"), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    total_hours = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, default="")


class MonthlyPaymentArchive(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    archive_year = db.Column(db.Integer, nullable=False)
    archive_month = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="current", nullable=False)  # current/outdated
    archived_total_amount = db.Column(db.Float, default=0.0, nullable=False)
    archived_entry_count = db.Column(db.Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("archive_year", "archive_month", name="uniq_monthly_payment_archive"),)


class Supervisor(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    role = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    can_approve_archive_refresh = db.Column(db.Boolean, default=True, nullable=False)


class RedBillingNotice(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    billing_advice_id = db.Column(db.Integer, db.ForeignKey("billing_advice.id"), nullable=False, unique=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    issued_date = db.Column(db.Date, nullable=False)
    outstanding_amount = db.Column(db.Float, default=0.0, nullable=False)
    next_session_date = db.Column(db.Date, nullable=True)
    red_bill_due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default="issued", nullable=False)
    manual_lift_active = db.Column(db.Boolean, default=False, nullable=False)
    manual_lift_reason = db.Column(db.String(255), default="", nullable=False)
    manual_lift_by_supervisor_id = db.Column(db.Integer, db.ForeignKey("supervisor.id"), nullable=True)
    manual_lift_at = db.Column(db.DateTime, nullable=True)

    billing_advice = db.relationship("BillingAdvice", backref="red_billing_notice")
    student = db.relationship("Student", backref="red_billing_notices")
    manual_lift_by_supervisor = db.relationship("Supervisor")


class AuditLog(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.Text, default="")


class WeeklyReportArchive(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    week_start = db.Column(db.Date, nullable=False)
    week_end = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(200), default="")

    __table_args__ = (UniqueConstraint("week_start", "week_end", name="uniq_week_archive"),)


class WeeklyReportArchiveItem(TimestampMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    archive_id = db.Column(db.Integer, db.ForeignKey("weekly_report_archive.id"), nullable=False)
    section = db.Column(db.String(20), nullable=False)  # student/therapist/admin
    reference_id = db.Column(db.Integer, nullable=True)
    reference_name = db.Column(db.String(120), nullable=False)
    hours = db.Column(db.Float, default=0.0, nullable=False)

    archive = db.relationship("WeeklyReportArchive", backref="items")
