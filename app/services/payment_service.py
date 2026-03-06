from __future__ import annotations

from datetime import date

from app.models import (
    AssessmentDepositLedger,
    AuditLog,
    BillingAdvice,
    Payment,
    PaymentAllocation,
    RequiredDepositLedger,
    Student,
    db,
)


ALLOCATION_ORDER = ["old_balance", "current_bill", "required_deposit", "assessment_deposit"]


def record_payment(student_id: int, amount: float, payment_date: date, notes: str = "") -> Payment:
    student = Student.query.get_or_404(student_id)
    remaining = round(amount, 2)

    with db.session.begin_nested():
        payment = Payment(student_id=student_id, amount=amount, payment_date=payment_date, notes=notes)
        db.session.add(payment)
        db.session.flush()

        open_advices = BillingAdvice.query.filter_by(student_id=student_id, status="Open").order_by(BillingAdvice.created_at).all()

        for advice in open_advices:
            if remaining <= 0:
                break
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

            req_unpaid = advice.required_deposit_charge
            req_applied = min(remaining, req_unpaid)
            if req_applied > 0:
                advice.required_deposit_charge -= req_applied
                advice.total_due -= req_applied
                student.required_deposit_paid += req_applied
                remaining -= req_applied
                db.session.add(RequiredDepositLedger(student_id=student_id, billing_cycle_id=advice.billing_cycle_id, billing_advice_id=advice.id, entry_type="paid", amount=req_applied))
                db.session.add(PaymentAllocation(payment_id=payment.id, billing_advice_id=advice.id, allocation_type="required_deposit", amount=req_applied))

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

        if remaining > 0:
            student.overpayment_credit = round(student.overpayment_credit + remaining, 2)
            db.session.add(PaymentAllocation(payment_id=payment.id, allocation_type="overpayment_credit", amount=remaining))

        db.session.add(AuditLog(action="payment_recorded", entity_type="Payment", entity_id=payment.id, details=f"amount={amount}"))

    db.session.commit()
    return payment
