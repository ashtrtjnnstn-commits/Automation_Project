from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from flask import Flask
from dotenv import load_dotenv

from .models import db
from .routes.web import web_bp
from .services.attendance_service import generate_monthly_sessions
from .services.billing_service import generate_billing_cycles_for_range, generate_billing_advices_for_cycle
from .services.seed_service import seed_sample_data
from .utils.backup_utils import backup_directory_for_database, backup_sqlite_database, resolve_sqlite_db_path


def _ensure_sqlite_parent_directory(app: Flask) -> None:
    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not database_uri.startswith("sqlite:///"):
        return

    db_path = Path(database_uri.replace("sqlite:///", "", 1))
    if not db_path.is_absolute():
        db_path = Path(app.instance_path) / db_path

    if db_path.parent.exists():
        app.logger.info("SQLite parent directory already exists: %s", db_path.parent)
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app.logger.info("Created missing SQLite parent directory: %s", db_path.parent)


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

    _ensure_sqlite_parent_directory(app)

    db.init_app(app)
    app.register_blueprint(web_bp)
    register_cli(app)

    with app.app_context():
        database_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        try:
            db_path = resolve_sqlite_db_path(database_uri)
            backup_dir = backup_directory_for_database(database_uri)
            app.logger.info("Startup backup trace db_path=%s backup_dir=%s", db_path, backup_dir)
        except ValueError:
            app.logger.info("Startup backup trace skipped for non-sqlite database uri=%s", database_uri)
        backup_path = backup_sqlite_database(database_uri)
        if backup_path:
            app.logger.info("Startup backup created: %s", backup_path)
        else:
            app.logger.warning("Startup backup not created for uri=%s", database_uri)

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
