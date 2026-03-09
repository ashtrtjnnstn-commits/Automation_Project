from __future__ import annotations

from datetime import date

from app.models import (
    AdminStaff,
    Student,
    Therapist,
    WeeklyReportArchive,
    WeeklyReportArchiveItem,
    db,
)
from app.services.admin_service import admin_hours_summary
from app.services.attendance_service import weekly_student_hours, weekly_therapist_hours


def archive_weekly_report(week_start: date, week_end: date, note: str = "") -> WeeklyReportArchive:
    existing = WeeklyReportArchive.query.filter_by(week_start=week_start, week_end=week_end).first()
    if existing:
        raise ValueError("Weekly report for this exact date range is already archived.")

    archive = WeeklyReportArchive(week_start=week_start, week_end=week_end, note=note)
    db.session.add(archive)
    db.session.flush()

    student_hours = weekly_student_hours(week_start, week_end)
    therapist_hours = weekly_therapist_hours(week_start, week_end)
    admin_hours = admin_hours_summary(week_start, week_end)

    for sid, hours in student_hours.items():
        student = Student.query.get(sid)
        db.session.add(
            WeeklyReportArchiveItem(
                archive_id=archive.id,
                section="student",
                reference_id=sid,
                reference_name=student.name if student else f"Student {sid}",
                hours=hours,
            )
        )

    for tid, hours in therapist_hours.items():
        therapist = Therapist.query.get(tid)
        db.session.add(
            WeeklyReportArchiveItem(
                archive_id=archive.id,
                section="therapist",
                reference_id=tid,
                reference_name=therapist.name if therapist else f"Therapist {tid}",
                hours=hours,
            )
        )

    for aid, hours in admin_hours.items():
        admin = AdminStaff.query.get(aid)
        db.session.add(
            WeeklyReportArchiveItem(
                archive_id=archive.id,
                section="admin",
                reference_id=aid,
                reference_name=admin.name if admin else f"Admin {aid}",
                hours=hours,
            )
        )

    db.session.commit()
    return archive


def get_archive_sections(archive: WeeklyReportArchive) -> dict[str, list[WeeklyReportArchiveItem]]:
    items = WeeklyReportArchiveItem.query.filter_by(archive_id=archive.id).order_by(WeeklyReportArchiveItem.section, WeeklyReportArchiveItem.reference_name).all()
    return {
        "student": [i for i in items if i.section == "student"],
        "therapist": [i for i in items if i.section == "therapist"],
        "admin": [i for i in items if i.section == "admin"],
    }
