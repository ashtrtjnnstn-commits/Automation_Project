from __future__ import annotations

from datetime import date, time

from app.models import AdminStaff, AdminAttendance, RegularSchedule, Student, Therapist, db
from app.services.billing_service import initialize_required_deposit


def seed_sample_data() -> None:
    if Therapist.query.first():
        return
    t1 = Therapist(name="Therapist Ana")
    t2 = Therapist(name="Therapist Ben")
    a1 = AdminStaff(name="Admin Claire")
    db.session.add_all([t1, t2, a1])
    db.session.flush()

    s1 = Student(name="Liam Cruz", assigned_therapist_id=t1.id, contract_hours_per_week=4, has_weekend_rate=False)
    s2 = Student(name="Mia Santos", assigned_therapist_id=t2.id, contract_hours_per_week=3, has_weekend_rate=True)
    db.session.add_all([s1, s2])
    db.session.flush()

    db.session.add_all([
        RegularSchedule(student_id=s1.id, therapist_id=t1.id, day_of_week=0, start_time=time(9, 0), end_time=time(11,0), duration_hours=2),
        RegularSchedule(student_id=s1.id, therapist_id=t1.id, day_of_week=2, start_time=time(9, 0), end_time=time(11,0), duration_hours=2),
        RegularSchedule(student_id=s2.id, therapist_id=t2.id, day_of_week=5, start_time=time(13, 0), end_time=time(14,30), duration_hours=1.5),
        RegularSchedule(student_id=s2.id, therapist_id=t2.id, day_of_week=6, start_time=time(13, 0), end_time=time(14,30), duration_hours=1.5),
    ])

    initialize_required_deposit(s1)
    initialize_required_deposit(s2)

    db.session.add(AdminAttendance(admin_id=a1.id, attendance_date=date.today(), status="Present", shift_label="AM", hours_worked=8))
    db.session.commit()
