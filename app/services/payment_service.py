from __future__ import annotations

from datetime import date

from sqlalchemy import String, cast, or_
from werkzeug.security import check_password_hash
from app.models import (
    AssessmentDepositLedger,
    BillingAdvice,
    MonthlyPaymentArchive,
    Payment,
    PaymentAllocation,
    RedBillingNotice,
    RequiredDepositLedger,
    Student,
    Supervisor,
    db,
)
from app.services.billing_service import initialize_required_deposit
from app.utils.audit_utils import log_audit


def _ensure_month_archive_snapshot(month: int, year: int) -> MonthlyPaymentArchive:
    snapshot = MonthlyPaymentArchive.query.filter_by(archive_month=month, archive_year=year).first()
    if not snapshot:
        snapshot = MonthlyPaymentArchive(archive_month=month, archive_year=year)
        db.session.add(snapshot)
    monthly_rows = Payment.query.filter_by(is_archived=True, archive_month=month, archive_year=year).all()
    snapshot.archived_total_amount = round(sum(p.amount for p in monthly_rows), 2)
    snapshot.archived_entry_count = len(monthly_rows)
    snapshot.status = "current"
    return snapshot


def mark_month_archive_outdated(month: int, year: int) -> None:
    snapshot = MonthlyPaymentArchive.query.filter_by(archive_month=month, archive_year=year).first()
    if snapshot:
        snapshot.status = "outdated"
        db.session.commit()


def mark_archive_outdated_if_needed_for_payment_date(payment_date: date) -> bool:
    month = payment_date.month
    year = payment_date.year
    snapshot = MonthlyPaymentArchive.query.filter_by(archive_month=month, archive_year=year).first()
    if snapshot and snapshot.status != "outdated":
        snapshot.status = "outdated"
        db.session.commit()
        return True
    return False


def refresh_month_archive_with_supervisor(month: int, year: int, supervisor_name: str, password: str, reason: str = "") -> tuple[bool, str]:
    supervisor = Supervisor.query.filter_by(name=supervisor_name, is_active=True).first()
    if not supervisor or not supervisor.can_approve_archive_refresh or not check_password_hash(supervisor.password_hash, password):
        return False, "Supervisor approval failed. Please check credentials and active status."

    snapshot = _ensure_month_archive_snapshot(month=month, year=year)
    db.session.commit()
    try:
        log_audit(
            "archive_refresh_approved",
            "monthly_payment_archive",
            snapshot.id,
            f"{year}-{month:02d}|approver={supervisor.name}|role={supervisor.role}|reason={reason}",
        )
    except Exception:
        pass
    return True, f"{date(year, month, 1).strftime('%B %Y')} archive refreshed successfully."


def month_archive_summary(month: int, year: int) -> dict[str, float | int]:
    snapshot = MonthlyPaymentArchive.query.filter_by(archive_month=month, archive_year=year).first()
    archived_total = snapshot.archived_total_amount if snapshot else 0.0
    archived_count = snapshot.archived_entry_count if snapshot else 0

    live_rows = Payment.query.filter(
        Payment.payment_date >= date(year, month, 1),
        Payment.payment_date < (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)),
    ).all()
    live_total = round(sum(p.amount for p in live_rows), 2)
    live_count = len(live_rows)
    return {
        "archived_total": archived_total,
        "archived_count": archived_count,
        "live_total": live_total,
        "live_count": live_count,
        "difference": round(live_total - archived_total, 2),
    }


def record_payment(
    student_id: int,
    amount: float,
    payment_date: date,
    notes: str = "",
    client_guardian_name: str = "",
    purpose: str = "Therapy",
    billing_period_start: date | None = None,
    billing_period_end: date | None = None,
    total_hours_rendered: float = 0.0,
    received_by_admin_id: int | None = None,
    mode_of_transfer: str = "Cash",
    manual_overpayment: float | None = None,
    manual_balance: float | None = None,
) -> Payment:
    student = Student.query.get_or_404(student_id)
    remaining = round(amount, 2)

    with db.session.begin_nested():
        payment = Payment(
            student_id=student_id,
            amount=amount,
            payment_date=payment_date,
            notes=notes,
            client_guardian_name=client_guardian_name,
            purpose=purpose,
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
            total_hours_rendered=total_hours_rendered,
            received_by_admin_id=received_by_admin_id,
            mode_of_transfer=mode_of_transfer,
        )
        db.session.add(payment)
        db.session.flush()

        open_advices = (
            BillingAdvice.query.filter(BillingAdvice.student_id == student_id, BillingAdvice.status.in_(["Draft", "Issued", "Open"]))
            .order_by(BillingAdvice.created_at)
            .all()
        )

        is_required_deposit_payment = purpose == "Required Deposit"
        is_assessment_deposit_payment = purpose == "Assessment Deposit"

        # Required deposit total is lazily initialized in billing flows; initialize here too
        # so direct deposit payments are applied to real obligation instead of overpayment.
        if is_required_deposit_payment:
            initialize_required_deposit(student)

        for advice in open_advices:
            if remaining <= 0:
                break
            if not is_required_deposit_payment and not is_assessment_deposit_payment:
                old_applied = min(remaining, advice.old_balance)
                if old_applied > 0:
                    advice.old_balance -= old_applied
                    advice.total_due -= old_applied
                    remaining -= old_applied
                    db.session.add(PaymentAllocation(payment_id=payment.id, billing_advice_id=advice.id, allocation_type="old_balance", amount=old_applied))

                current_due = max(advice.total_due - advice.required_deposit_charge - advice.assessment_deposit_charge, 0)
                cur_applied = min(remaining, current_due)
                if cur_applied > 0:
                    advice.total_due -= cur_applied
                    remaining -= cur_applied
                    db.session.add(PaymentAllocation(payment_id=payment.id, billing_advice_id=advice.id, allocation_type="current_bill", amount=cur_applied))

            if is_required_deposit_payment or (not is_assessment_deposit_payment):
                req_unpaid = advice.required_deposit_charge
                req_applied = min(remaining, req_unpaid)
                if req_applied > 0:
                    advice.required_deposit_charge -= req_applied
                    advice.total_due -= req_applied
                    student.required_deposit_paid += req_applied
                    remaining -= req_applied
                    db.session.add(RequiredDepositLedger(student_id=student_id, billing_cycle_id=advice.billing_cycle_id, billing_advice_id=advice.id, entry_type="paid", amount=req_applied))
                    db.session.add(PaymentAllocation(payment_id=payment.id, billing_advice_id=advice.id, allocation_type="required_deposit", amount=req_applied))

            if is_assessment_deposit_payment or (not is_required_deposit_payment):
                ass_unpaid = advice.assessment_deposit_charge
                ass_applied = min(remaining, ass_unpaid)
                if ass_applied > 0:
                    advice.assessment_deposit_charge -= ass_applied
                    advice.total_due -= ass_applied
                    student.assessment_deposit_paid += ass_applied
                    remaining -= ass_applied
                    db.session.add(AssessmentDepositLedger(student_id=student_id, billing_cycle_id=advice.billing_cycle_id, billing_advice_id=advice.id, entry_type="paid", amount=ass_applied))
                    db.session.add(PaymentAllocation(payment_id=payment.id, billing_advice_id=advice.id, allocation_type="assessment_deposit", amount=ass_applied))

            if advice.total_due <= 0.009:
                advice.total_due = 0.0
                advice.status = "Paid"

        if is_required_deposit_payment and remaining > 0:
            direct_req_unpaid = max(student.required_deposit_total - student.required_deposit_paid, 0)
            direct_req_applied = min(remaining, direct_req_unpaid)
            if direct_req_applied > 0:
                student.required_deposit_paid += direct_req_applied
                remaining -= direct_req_applied
                db.session.add(RequiredDepositLedger(student_id=student_id, entry_type="paid", amount=direct_req_applied))
                db.session.add(PaymentAllocation(payment_id=payment.id, allocation_type="required_deposit", amount=direct_req_applied))

        if is_assessment_deposit_payment and remaining > 0:
            direct_ass_unpaid = max(student.assessment_deposit_total - student.assessment_deposit_paid, 0)
            direct_ass_applied = min(remaining, direct_ass_unpaid)
            if direct_ass_applied > 0:
                student.assessment_deposit_paid += direct_ass_applied
                remaining -= direct_ass_applied
                db.session.add(AssessmentDepositLedger(student_id=student_id, entry_type="paid", amount=direct_ass_applied))
                db.session.add(PaymentAllocation(payment_id=payment.id, allocation_type="assessment_deposit", amount=direct_ass_applied))

        if remaining > 0:
            student.overpayment_credit = round(student.overpayment_credit + remaining, 2)
            payment.overpayment_amount = remaining
            db.session.add(PaymentAllocation(payment_id=payment.id, allocation_type="overpayment_credit", amount=remaining))

        if is_required_deposit_payment:
            payment.balance_after_payment = round(max(student.required_deposit_total - student.required_deposit_paid, 0), 2)
        elif is_assessment_deposit_payment:
            payment.balance_after_payment = round(max(student.assessment_deposit_total - student.assessment_deposit_paid, 0), 2)
        else:
            latest_open = (
                BillingAdvice.query.filter(BillingAdvice.student_id == student_id, BillingAdvice.status.in_(["Draft", "Issued", "Open"]))
                .all()
            )
            payment.balance_after_payment = round(sum(a.total_due for a in latest_open), 2)

        if manual_overpayment is not None:
            payment.overpayment_amount = manual_overpayment
        if manual_balance is not None:
            payment.balance_after_payment = manual_balance

    db.session.commit()
    _sync_red_billing_after_settlement(student_id)
    try:
        log_audit("payment_recorded", "Payment", payment.id, f"amount={amount}")
    except Exception:
        pass
    mark_archive_outdated_if_needed_for_payment_date(payment.payment_date)
    return payment


def _sync_red_billing_after_settlement(student_id: int) -> None:
    notices = RedBillingNotice.query.filter_by(student_id=student_id).all()
    changed = False
    for notice in notices:
        advice = notice.billing_advice
        if not advice:
            continue
        notice.outstanding_amount = advice.total_due
        if advice.total_due <= 0:
            notice.status = "settled"
            notice.manual_lift_active = False
        else:
            notice.status = "issued"
        changed = True
    if changed:
        db.session.commit()


def apply_payment_search(query, search_query: str):
    term = (search_query or "").strip()
    if not term:
        return query

    like = f"%{term}%"
    return query.filter(
        or_(
            Student.name.ilike(like),
            Payment.client_guardian_name.ilike(like),
            Payment.purpose.ilike(like),
            cast(Payment.billing_period_start, String).ilike(like),
            cast(Payment.billing_period_end, String).ilike(like),
        )
    )


def archive_payments(month: int, year: int) -> int:
    already_archived = Payment.query.filter_by(is_archived=True, archive_month=month, archive_year=year).first()
    if already_archived:
        raise ValueError(f"Archive for {year}-{month:02d} already exists.")

    payments = Payment.query.filter(
        Payment.is_archived.is_(False),
        Payment.payment_date >= date(year, month, 1),
        Payment.payment_date < (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)),
    ).all()
    for p in payments:
        p.is_archived = True
        p.archive_month = month
        p.archive_year = year
    _ensure_month_archive_snapshot(month=month, year=year)
    db.session.commit()
    return len(payments)
