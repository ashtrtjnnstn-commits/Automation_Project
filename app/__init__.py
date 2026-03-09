from __future__ import annotations

import os
from datetime import date

from flask import Flask
from dotenv import load_dotenv

from .models import db
from .routes.web import web_bp
from .services.attendance_service import generate_monthly_sessions
from .services.billing_service import generate_billing_cycles_for_range, generate_billing_advices_for_cycle
from .services.seed_service import seed_sample_data


def create_app(test_config: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///data/app.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    app.register_blueprint(web_bp)
    register_cli(app)

    return app


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db() -> None:
        db.create_all()
        print("Database tables created.")

    @app.cli.command("seed-data")
    def seed_data() -> None:
        seed_sample_data()
        print("Sample data inserted.")

    @app.cli.command("generate-month")
    def generate_month_cmd() -> None:
        today = date.today()
        count = generate_monthly_sessions(today.year, today.month)
        print(f"Generated {count} sessions for {today.year}-{today.month:02d}.")

    @app.cli.command("generate-billing")
    def generate_billing_cmd() -> None:
        today = date.today()
        cycles = generate_billing_cycles_for_range(today.replace(day=1), today)
        for cycle in cycles:
            generate_billing_advices_for_cycle(cycle.id)
        print(f"Processed {len(cycles)} billing cycle(s).")
