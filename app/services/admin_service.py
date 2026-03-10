from __future__ import annotations

from collections import defaultdict
from datetime import date

from app.models import AdminAttendance


def admin_hours_summary(start_date: date, end_date: date) -> dict[int, float]:
    rows = AdminAttendance.query.filter(
        AdminAttendance.attendance_date >= start_date,
        AdminAttendance.attendance_date <= end_date,
        AdminAttendance.status == "Present",
    ).all()
    totals = defaultdict(float)
    for row in rows:
        totals[row.admin_id] += row.hours_worked
    return dict(totals)
