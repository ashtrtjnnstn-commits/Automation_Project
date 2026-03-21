from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from app.utils.audit_utils import log_audit

logger = logging.getLogger(__name__)


def restore_sqlite_backup(database_uri: str, backup_path: str | Path) -> Path:
    """Restore sqlite database from a backup file.

    Note: restoring while the app is running can be unsafe; stop the app before using this.
    """
    if not database_uri.startswith("sqlite:///"):
        raise ValueError("Restore is only supported for sqlite file databases.")

    db_path = Path(database_uri.replace("sqlite:///", "", 1))
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    selected_backup = Path(backup_path)
    if not selected_backup.exists() or not selected_backup.is_file():
        raise FileNotFoundError("Selected backup file was not found.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(selected_backup, db_path)
    log_audit("backup_restored", "Database", None, str(selected_backup))
    return db_path


def backup_sqlite_database(database_uri: str, keep: int = 7) -> Path | None:
    """Create timestamped backup for sqlite file DB and prune old backups."""
    try:
        if not database_uri.startswith("sqlite:///"):
            return None

        db_path = Path(database_uri.replace("sqlite:///", "", 1))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path

        if not db_path.exists():
            return None

        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"app_{stamp}.db"
        shutil.copy2(db_path, backup_path)

        backups = sorted(backup_dir.glob("app_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            old.unlink(missing_ok=True)

        log_audit("backup_created", "Database", None, str(backup_path))
        return backup_path
    except Exception:
        logger.exception("Automatic DB backup failed")
        return None
