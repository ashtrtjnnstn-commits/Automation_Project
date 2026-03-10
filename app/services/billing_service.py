from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.models import (
    AssessmentDepositLedger,
    AttendanceSession,
    AuditLog,
    BillingAdvice,
    BillingCycle,
    BillingLineItem,
    Payment,
    PaymentAllocation,
    RequiredDepositLedger,
    SessionOverride,
    Student,
    db,
)
BILLABLE_STATUSES = {"Present", "Make-up", "Rescheduled", "Billed"}
RENDERED_BILLING_STATUSES = {"Present", "Make-up", "Rescheduled", "Billed", "Non-billable"}

WEEKDAY_RATE = 550.0
WEEKEND_RATE = 600.0


@dataclass
class SessionTotals:
    weekday_hours: float = 0.0
    weekend_hours: float = 0.0
    weekday_amount: float = 0.0
    weekend_amount: float = 0.0


def generate_billing_cycles_for_range(range_start: date, range_end: date) -> list[BillingCycle]:
    cycles: list[BillingCycle] = []
    cursor = range_start
    while cursor <= range_end:
        cycle_end = min(cursor + timedelta(days=14), range_end)
        existing = BillingCycle.query.filter_by(start_date=cursor, end_date=cycle_end).first()
        if existing:
            cycles.append(existing)
        else:
            cycle = BillingCycle(
                start_date=cursor,
                end_date=cycle_end,
                issue_date=cycle_end,
                due_date=cycle_end + timedelta(days=5),
            )
            db.session.add(cycle)
            db.session.flush()
            cycles.append(cycle)
        cursor = cycle_end + timedelta(days=1)
    db.session.commit()
    return cycles


def _required_deposit_rate(student: Student) -> float:
    """Fair blended rate from student's regular schedules."""
    weekday_count = sum(1 for s in student.regular_schedules if s.day_of_week < 5)
    weekend_count = sum(1 for s in student.regular_schedules if s.day_of_week >= 5)
    total = weekday_count + weekend_count
    if total == 0:
        return WEEKDAY_RATE
    return ((weekday_count * WEEKDAY_RATE) + (weekend_count * WEEKEND_RATE)) / total


def initialize_required_deposit(student: Student) -> None:
    if student.required_deposit_total > 0:
        return
    rate = _required_deposit_rate(student)
    student.required_deposit_total = round(student.contract_hours_per_week * rate * 2, 2)


def _session_totals(student_id: int, start_date: date, end_date: date) -> SessionTotals:
    sessions = _effective_sessions(student_id, start_date, end_date, BILLABLE_STATUSES)
    totals = SessionTotals()
    for s in sessions:
        if s.session_date.weekday() < 5:
            totals.weekday_hours += s.duration_hours
            totals.weekday_amount += s.duration_hours * WEEKDAY_RATE
        else:
            totals.weekend_hours += s.duration_hours
            totals.weekend_amount += s.duration_hours * WEEKEND_RATE
    return totals


def _effective_sessions(student_id: int, start_date: date, end_date: date, statuses: set[str]) -> list[AttendanceSession]:
    replaced_ids = {
        row[0]
        for row in db.session.query(SessionOverride.original_session_id)
        .filter(SessionOverride.original_session_id.isnot(None))
        .all()
    }

    sessions = AttendanceSession.query.filter(
        AttendanceSession.student_id == student_id,
        AttendanceSession.session_date >= start_date,
        AttendanceSession.session_date <= end_date,
        AttendanceSession.status.in_(list(statuses)),
    ).all()

    return [s for s in sessions if s.id not in replaced_ids]


def billing_hours_breakdown(student_id: int, start_date: date, end_date: date) -> dict[str, float]:
    sessions = _effective_sessions(student_id, start_date, end_date, RENDERED_BILLING_STATUSES)
    regular_hours = 0.0
    makeup_hours = 0.0
    billed_hours = 0.0
    non_billable_hours = 0.0

    for s in sessions:
        if s.status == "Non-billable":
            non_billable_hours += s.duration_hours
        elif s.status == "Billed":
            billed_hours += s.duration_hours
        elif s.session_type == "makeup":
            makeup_hours += s.duration_hours
        else:
            regular_hours += s.duration_hours

    billable_hours = regular_hours + makeup_hours + billed_hours
    total_rendered_hours = billable_hours + non_billable_hours
    return {
        "regular_rendered_hours": round(regular_hours, 2),
        "makeup_rendered_hours": round(makeup_hours, 2),
        "billed_hours": round(billed_hours, 2),
        "total_rendered_hours": round(total_rendered_hours, 2),
        "billable_hours": round(billable_hours, 2),
        "non_billable_hours": round(non_billable_hours, 2),
    }


def billing_hours_breakdown_for_advice(advice: BillingAdvice) -> dict[str, float]:
    hours = billing_hours_breakdown(advice.student_id, advice.billing_cycle.start_date, advice.billing_cycle.end_date)
    override_map = {
        "regular_rendered_hours": "regular_rendered_hours",
        "makeup_rendered_hours": "makeup_rendered_hours",
        "billed_hours": "billed_hours",
        "total_rendered_hours": "total_rendered_hours",
        "billable_hours": "billable_hours",
        "non_billable_hours": "non_billable_hours",
    }
    for item in advice.line_items:
        key = override_map.get(item.item_type)
        if key:
            hours[key] = round(item.quantity, 2)
    return hours


def _unpaid_previous_balance(student_id: int, exclude_cycle_id: int | None = None) -> float:
    query = BillingAdvice.query.filter_by(student_id=student_id, status="Open")
    if exclude_cycle_id is not None:
        query = query.filter(BillingAdvice.billing_cycle_id != exclude_cycle_id)
    open_advices = query.all()
    return round(sum(a.total_due for a in open_advices), 2)


def _required_deposit_charge(student: Student) -> float:
    initialize_required_deposit(student)
    billed_total = _required_billed_total(student)
    paid_total = _required_paid_total(student)

    remaining_by_billed = max(student.required_deposit_total - billed_total, 0)
    remaining_by_paid = max(student.required_deposit_total - paid_total, 0)
    remaining = min(remaining_by_billed, remaining_by_paid)
    if remaining <= 0:
        return 0.0
    cycle_count = RequiredDepositLedger.query.filter_by(student_id=student.id, entry_type="billed").count()
    if cycle_count >= 4:
        return 0.0
    charge = round(student.required_deposit_total / 4, 2)
    return min(charge, remaining)


def _assessment_deposit_charge(student: Student) -> float:
    billed_total = _assessment_billed_total(student)
    paid_total = _assessment_paid_total(student)

    remaining_by_billed = max(student.assessment_deposit_total - billed_total, 0)
    remaining_by_paid = max(student.assessment_deposit_total - paid_total, 0)
    remaining = min(remaining_by_billed, remaining_by_paid)
    if remaining <= 0:
        return 0.0
    return min(2500.0, remaining)


def _true_general_credit(student: Student) -> float:
    tracked_required_paid = _required_tracked_paid_total(student)
    tracked_assessment_paid = _assessment_tracked_paid_total(student)
    converted_required = max(_required_paid_total(student) - tracked_required_paid, 0)
    converted_assessment = max(_assessment_paid_total(student) - tracked_assessment_paid, 0)
    return round(max(student.overpayment_credit - converted_required - converted_assessment, 0), 2)


def _required_billed_total(student: Student) -> float:
    billed_from_ledger = (
        db.session.query(db.func.coalesce(db.func.sum(RequiredDepositLedger.amount), 0.0))
        .filter_by(student_id=student.id, entry_type="billed")
        .scalar()
    )
    return max(student.required_deposit_billed, billed_from_ledger or 0.0)


def _assessment_billed_total(student: Student) -> float:
    billed_from_ledger = (
        db.session.query(db.func.coalesce(db.func.sum(AssessmentDepositLedger.amount), 0.0))
        .filter_by(student_id=student.id, entry_type="billed")
        .scalar()
    )
    return max(student.assessment_deposit_billed, billed_from_ledger or 0.0)


def _required_tracked_paid_total(student: Student) -> float:
    paid_from_ledger = (
        db.session.query(db.func.coalesce(db.func.sum(RequiredDepositLedger.amount), 0.0))
        .filter_by(student_id=student.id, entry_type="paid")
        .scalar()
    )
    paid_from_alloc = (
        db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0.0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(Payment.student_id == student.id, PaymentAllocation.allocation_type == "required_deposit")
        .scalar()
    )
    return max(student.required_deposit_paid, paid_from_ledger or 0.0, paid_from_alloc or 0.0)


def _assessment_tracked_paid_total(student: Student) -> float:
    paid_from_ledger = (
        db.session.query(db.func.coalesce(db.func.sum(AssessmentDepositLedger.amount), 0.0))
        .filter_by(student_id=student.id, entry_type="paid")
        .scalar()
    )
    paid_from_alloc = (
        db.session.query(db.func.coalesce(db.func.sum(PaymentAllocation.amount), 0.0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(Payment.student_id == student.id, PaymentAllocation.allocation_type == "assessment_deposit")
        .scalar()
    )
    return max(student.assessment_deposit_paid, paid_from_ledger or 0.0, paid_from_alloc or 0.0)


def _required_paid_total(student: Student) -> float:
    tracked = _required_tracked_paid_total(student)
    paid_from_purpose = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0.0))
        .filter(Payment.student_id == student.id, Payment.purpose == "Required Deposit")
        .scalar()
    )
    inferred = min(student.required_deposit_total, paid_from_purpose or 0.0)
    return max(tracked, inferred)


def _assessment_paid_total(student: Student) -> float:
    tracked = _assessment_tracked_paid_total(student)
    paid_from_purpose = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0.0))
        .filter(Payment.student_id == student.id, Payment.purpose == "Assessment Deposit")
        .scalar()
    )
    inferred = min(student.assessment_deposit_total, paid_from_purpose or 0.0)
    return max(tracked, inferred)


def generate_billing_advices_for_cycle(cycle_id: int, student_id: int | None = None) -> list[BillingAdvice]:
    """Generate billing advice for one cycle.

    If student_id is provided, only that student is calculated.
    """
    cycle = BillingCycle.query.get_or_404(cycle_id)
    if student_id:
        students = Student.query.filter_by(id=student_id, active=True).all()
    else:
        students = Student.query.filter_by(active=True).all()

    created: list[BillingAdvice] = []

    with db.session.begin_nested():
        for student in students:
            totals = _session_totals(student.id, cycle.start_date, cycle.end_date)
            subtotal = round(totals.weekday_amount + totals.weekend_amount, 2)
            old_balance = _unpaid_previous_balance(student.id, exclude_cycle_id=cycle.id)
            required_charge = _required_deposit_charge(student)
            assessment_charge = _assessment_deposit_charge(student)
            credit = _true_general_credit(student)
            total_due = round(max(subtotal + old_balance + required_charge + assessment_charge - credit, 0), 2)

            advice = BillingAdvice.query.filter_by(student_id=student.id, billing_cycle_id=cycle.id).first()
            if not advice:
                advice = BillingAdvice(student_id=student.id, billing_cycle_id=cycle.id)
            advice.subtotal_sessions = subtotal
            advice.old_balance = old_balance
            advice.overpayment_credit = credit
            advice.required_deposit_charge = required_charge
            advice.assessment_deposit_charge = assessment_charge
            advice.total_due = total_due
            advice.status = "Open"
            db.session.add(advice)
            db.session.flush()

            BillingLineItem.query.filter_by(billing_advice_id=advice.id).delete()
            db.session.add(BillingLineItem(billing_advice_id=advice.id, item_type="weekday", description="Weekday sessions", quantity=totals.weekday_hours, rate=WEEKDAY_RATE, amount=totals.weekday_amount))
            db.session.add(BillingLineItem(billing_advice_id=advice.id, item_type="weekend", description="Weekend sessions", quantity=totals.weekend_hours, rate=WEEKEND_RATE, amount=totals.weekend_amount))
            if required_charge:
                student.required_deposit_billed += required_charge
                db.session.add(RequiredDepositLedger(student_id=student.id, billing_cycle_id=cycle.id, billing_advice_id=advice.id, entry_type="billed", amount=required_charge))
            if assessment_charge:
                student.assessment_deposit_billed += assessment_charge
                db.session.add(AssessmentDepositLedger(student_id=student.id, billing_cycle_id=cycle.id, billing_advice_id=advice.id, entry_type="billed", amount=assessment_charge))
            student.overpayment_credit = 0.0
            created.append(advice)

        db.session.add(AuditLog(action="billing_regeneration", entity_type="BillingCycle", entity_id=cycle.id, details=f"Advices generated for {'all students' if not student_id else f'student {student_id}'}"))
    db.session.commit()
    return created


def due_summary(today: date) -> dict[str, list[BillingAdvice]]:
    due_today = BillingAdvice.query.join(BillingCycle).filter(BillingAdvice.status == "Open", BillingCycle.due_date == today).all()
    overdue = BillingAdvice.query.join(BillingCycle).filter(BillingAdvice.status == "Open", BillingCycle.due_date < today).all()
    upcoming = BillingAdvice.query.join(BillingCycle).filter(
        BillingAdvice.status == "Open",
        BillingCycle.due_date > today,
        BillingCycle.due_date <= today + timedelta(days=3),
    ).all()
    return {"due_today": due_today, "overdue": overdue, "upcoming": upcoming}
