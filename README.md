# Therapy Center Attendance and Billing System

A clean rebuild of a therapy center operations app with a local Flask server, SQLite database, browser UI, and Excel import/export.

## Patch Notes

### Phase 1 Hardening
- Automatic local database backups now run on app startup with retention of the latest 7 backups.
- Billing advice can now be locked by status: Draft, Issued, Paid, Archived.
- Added audit log tracking for key operational actions.

### Version 9.6
- Billing advice can now be edited from the dashboard.
- Billing advice can now be deleted from the dashboard.
- Added attendance status "Billed", which is counted in billing advice and billing breakdowns.

### Version 9.5 (Stabilization)
- Weekly missed vs make-up recovery now has a per-student view to prevent pooled/misleading recovery totals.
- Billing now consistently includes effective rendered billable session charges for the selected billing period.
- Billing advice output now clearly shows rendered-hour basis (regular, make-up, total rendered, billable, non-billable).
- Deposit payments are now applied to deposit obligations correctly and no longer misclassified as full overpayments.
- Fixed structural billing bug where deposit-paid amounts were incorrectly reused as general credit and zeroed-out new billing advice totals.

### Version 9.3
- Added Excel export for Required Deposit payment history (all students and per student).
- Added Excel export for Assessment Deposit payment history (all students and per student).
- Billing Advice export now includes separate rendered hours columns for regular vs make-up sessions.
- Added missed-vs-recovered make-up hours summary (missed, recovered, remaining) in weekly reports and student profile.

### Version 9.2
- Billing advice now respects **paid required deposit** amounts when computing new required deposit charges.
- Billing advice now respects **paid assessment deposit** amounts when computing new assessment deposit charges.
- Billing advice session subtotal now consistently computes billable rendered sessions (Present/Make-up/Rescheduled/Billed) using weekday/weekend rates.
- Payment tracker now supports editing existing payment ledger entries.

## Tech Stack
- Python 3.11+
- Flask + SQLAlchemy ORM
- SQLite
- Jinja templates + vanilla HTML/CSS
- openpyxl for Excel import/export
- pytest for tests

## Project Tree
```text
.
├── .env.example
├── README.md
├── requirements.txt
├── server.py
├── app
│   ├── __init__.py
│   ├── models.py
│   ├── routes
│   │   └── web.py
│   ├── services
│   │   ├── admin_service.py
│   │   ├── attendance_service.py
│   │   ├── billing_service.py
│   │   ├── import_export_service.py
│   │   ├── payment_service.py
│   │   └── seed_service.py
│   ├── static
│   │   └── styles.css
│   ├── templates
│   │   ├── admin_attendance.html
│   │   ├── attendance_month.html
│   │   ├── base.html
│   │   ├── billing.html
│   │   ├── daily_schedule.html
│   │   ├── dashboard.html
│   │   ├── export_reports.html
│   │   ├── import.html
│   │   ├── master_admins.html
│   │   ├── master_data_index.html
│   │   ├── master_schedules.html
│   │   ├── master_students.html
│   │   ├── master_therapists.html
│   │   ├── makeup_editor.html
│   │   ├── payments.html
│   │   ├── payments_tracker.html
│   │   ├── student_profile.html
│   │   ├── therapist_profile.html
│   │   ├── weekly_archive_view.html
│   │   └── weekly_reports.html
│   └── utils
│       └── date_utils.py
├── data
├── exports
└── tests
    ├── conftest.py
    └── test_system.py
```

## Features

### Attendance
- Monthly session generation from normalized `RegularSchedule` records.
- Calendar-style monthly list grouped by date.
- Session statuses: Present, Absent, Cancelled, Make-up, Rescheduled, No Show, Non-billable, Billed.
- Make-up/override flow with audit trail and linked override records.
- Weekly rendered hour summaries for students and therapists.
- Admin attendance tracking and summary support.

### Billing (15-day cycles)
- Auto-create 15-day cutoff cycles (supports month boundaries).
- Advice generated from **actual rendered sessions**.
- Weekday rate: 550 PHP; Weekend rate: 600 PHP.
- Due date automatically set to issue date + 5 days.
- Carries old balance and applies overpayment credit.

### Deposit logic
- **Required deposit total formula:**
  `contract_hours_per_week * blended_rate * 2 weeks`
- **Blended rate rule:** weighted by weekday vs weekend recurring schedule entries.
- Required deposit charged in up to 4 billing cycles (`total/4` each cycle), stops after 4 or full amount.
- Assessment deposit set to 5000 and billed at 2500 per cycle until complete.
- Both deposits tracked in separate ledger tables for billed/paid entries.

### Payment tracker
- Payments Input form now stores ledger fields: Date, Client/Guardian, Student, Purpose, Billing Period, Total Hours Rendered, Amount Paid, Received By, Overpayment, Balance, and Mode of Transfer.
- Separate Payments Dashboard/Tracker page shows the monthly operational ledger table.
- Supports month-level archiving of payment records without deleting historical data.
- Keeps existing allocation logic for balances/deposits and shows resulting overpayment/balance values in the ledger.

### Excel import/export
- Import students + regular schedules from `.xlsx`.
- Export attendance summary to Excel.
- Export therapist weekly hours to Excel.
- Export billing advice and payment ledger to Excel.
- Export admin attendance summary to Excel.

### Auditability
- Logs key manual/system operations:
  - month generation
  - status changes
  - make-up creation
  - billing regeneration
  - payment recording

## Setup

1. Create virtual environment and install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Optional env config:
```bash
cp .env.example .env
```

3. Initialize DB:
```bash
flask --app server.py init-db
```

4. Seed sample data:
```bash
flask --app server.py seed-data
```

5. Run app:
```bash
python server.py
```
Open: `http://127.0.0.1:5000`

## Usage Guide

### Import schedules and staff info
Go to **Import** page and choose dataset type:

1) **Students + Regular Schedules**
- Student Name
- Therapist
- Day
- Start Time (HH:MM)
- Duration Hours
- Contract Hours

2) **Admin Staff**
- Admin Name
- Active (Yes/No)

3) **Therapists**
- Therapist Name
- Active (Yes/No)

### Generate monthly attendance
Go to **Attendance** page:
- choose year/month
- click **Generate Month Sessions**
- monthly page shows rendered vs upcoming status (upcoming stays blank)
- use **Daily Schedule** page to set status using dropdown
- click one **Update Attendance Statuses** button at bottom of Daily Schedule page

### Add make-up session
Go to **Make-up Editor**:
- select student/therapist/date/time/duration
- optional original session ID
- save override

### Generate billing
Go to **Billing**:
- select one student first (loads student-specific billing context)
- then enter start/end dates
- generate 15-day cycle advice for that selected student only
- page shows student context, rendered hours, rates, deposits, previous balance/credit, and resulting total due

### Record payment
Go to **Payments**:
- fill ledger fields (Date, Guardian/Client, Student, Purpose, Billing Period, Hours, Amount, Received By, Overpayment, Balance, Mode)
- submit payment; system keeps allocation logic and computes/stores overpayment and balance snapshot
- use **Payments Tracker** tab for current-month ledger filtering and archived-month retrieval
- use archive form in tracker to archive one month (kept retrievable)

### Dashboard due alerts
Dashboard shows:
- due today
- overdue
- upcoming (within 3 days)

### Remove / clear / edit imported records (admin maintenance)
Go to **Import** page and use the **Clear Selected Data** form.
- Type `CLEAR` to confirm before deletion.
- Available cleanup targets:
  - Students + schedules + attendance + billing advice
  - Therapists
  - Admin staff + admin attendance

To edit imported records, re-import corrected files for new entries and use profile pages + attendance/billing/payment pages for operational edits.

### Exports
Use **Export Reports** page to export:
- attendance summary
- therapist weekly hours
- billing advice
- payment ledger
- admin attendance

### Master data management
Use **Master Data** tab to manage core records in-app (no Excel required):
- Students (add/edit/active)
- Therapists (add/edit/active)
- Admin Staff (add/edit/active)
- Regular Schedules (add/edit/active with validation)

### Weekly report archiving
From **Weekly Reports**:
- load a week
- click **Archive This Week** to freeze a snapshot
- open archived entries from the archive list
- archived view uses stored snapshot rows (not live recomputation)

## Tests
Run:
```bash
pytest
```

Coverage includes:
- monthly session generation
- make-up session creation
- weekly student and therapist hours
- billing cycle generation and due-date rule
- weekday/weekend billing behavior
- required deposit stop after 4 cycles
- assessment deposit billing
- partial payment allocation
- overpayment carry-forward

## Reasonable assumptions documented
- Attendance base unit is 30 minutes but durations are stored as decimal hours; UI uses 0.5 step inputs.
- Rendered sessions are statuses: Present, Make-up, Rescheduled, Billed.
- If no regular schedule exists, required deposit fallback uses weekday rate.
- This build is intentionally explicit and maintainable over abstract architecture.


## Manual step after pulling schema changes
New tables/columns were added (`RegularSchedule.end_time`, `WeeklyReportArchive`, `WeeklyReportArchiveItem`). Reinitialize local DB if needed:

```bash
rm -f data/app.db
flask --app server.py init-db
flask --app server.py seed-data
```
