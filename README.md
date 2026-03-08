# Therapy Center Attendance and Billing System

A clean rebuild of a therapy center operations app with a local Flask server, SQLite database, browser UI, and Excel import/export.

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
│   │   ├── makeup_editor.html
│   │   ├── payments.html
│   │   ├── student_profile.html
│   │   ├── therapist_profile.html
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
- Session statuses: Present, Absent, Cancelled, Make-up, Rescheduled, No Show.
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
- Records payment + transparent allocations:
  - old balance
  - current bill
  - required deposit
  - assessment deposit
  - leftover overpayment credit
- Unpaid balances remain visible and carry forward.
- Overpayments become student-level credit for next billing advice.

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

### Import schedules
Go to **Import** page and upload an Excel file with columns:
- Student Name
- Therapist
- Day
- Start Time (HH:MM)
- Duration Hours
- Contract Hours

### Generate monthly attendance
Go to **Attendance** page:
- choose year/month
- click **Generate Month Sessions**
- edit statuses directly per session

### Add make-up session
Go to **Make-up Editor**:
- select student/therapist/date/time/duration
- optional original session ID
- save override

### Generate billing
Go to **Billing**:
- choose start/end dates
- generate cycles + advice

### Record payment
Go to **Payments**:
- select student
- enter payment date and amount
- system auto-allocates amount by rules

### Dashboard due alerts
Dashboard shows:
- due today
- overdue
- upcoming (within 3 days)

### Exports
Use **Export Reports** page to export:
- attendance summary
- therapist weekly hours
- billing advice
- payment ledger
- admin attendance

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
- Rendered sessions are statuses: Present, Make-up, Rescheduled.
- If no regular schedule exists, required deposit fallback uses weekday rate.
- This build is intentionally explicit and maintainable over abstract architecture.
