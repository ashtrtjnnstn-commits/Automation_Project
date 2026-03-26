from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from app.utils.audit_utils import log_audit

logger = logging.getLogger(__name__)


def resolve_sqlite_db_path(database_uri: str) -> Path:
    if not database_uri.startswith("sqlite:///"):
        raise ValueError("Restore is only supported for sqlite file databases.")

    db_path = Path(database_uri.replace("sqlite:///", "", 1))
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    return db_path


def validate_backup_filename(filename: str) -> None:
    path_name = Path(filename)
    if filename != path_name.name or not filename.startswith("app_") or path_name.suffix != ".db":
        raise ValueError("Invalid backup filename.")


def backup_directory_for_database(database_uri: str) -> Path:
    return resolve_sqlite_db_path(database_uri).parent / "backups"


def list_sqlite_backups(database_uri: str) -> list[Path]:
    backup_dir = backup_directory_for_database(database_uri)
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("app_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def _is_readable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.R_OK)


def restore_sqlite_backup(database_uri: str, backup_path: str | Path, create_emergency_backup: bool = True) -> tuple[Path, Path | None]:
    """Restore sqlite database from a backup file.

    Note: restoring while the app is running can be unsafe; stop the app before using this.
    """
    db_path = resolve_sqlite_db_path(database_uri)

    selected_backup = Path(backup_path)
    if not _is_readable_file(selected_backup):
        raise FileNotFoundError("Selected backup file was not found or is not readable.")

    if not db_path.exists() or not db_path.is_file():
        raise FileNotFoundError("Restore failed. Live database file could not be located.")
    if not os.access(db_path, os.W_OK):
        raise PermissionError("Restore failed. Live database file is not writable.")

    emergency_backup = None
    if create_emergency_backup:
        emergency_dir = db_path.parent / "backups"
        emergency_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        emergency_backup = emergency_dir / f"pre_restore_{stamp}.db"
        shutil.copy2(db_path, emergency_backup)

    shutil.copy2(selected_backup, db_path)
    log_audit("backup_restored", "Database", None, f"source={selected_backup}|target={db_path}")
    return db_path, emergency_backup


def backup_sqlite_database(database_uri: str, keep: int = 7) -> Path | None:
    """Create timestamped backup for sqlite file DB and prune old backups."""
    try:
        db_path = resolve_sqlite_db_path(database_uri)
        backup_dir = backup_directory_for_database(database_uri)

        if not db_path.exists():
            logger.warning("Backup skipped because live sqlite DB was not found: %s", db_path)
            return None

        backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Ensured backup directory exists: %s", backup_dir)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"app_{stamp}.db"
        shutil.copy2(db_path, backup_path)
        logger.info("Created sqlite backup: %s", backup_path)

        backups = list_sqlite_backups(database_uri)
        for old in backups[keep:]:
            old.unlink(missing_ok=True)

        log_audit("backup_created", "Database", None, str(backup_path))
        return backup_path
    except ValueError:
        return None
    except Exception:
        logger.exception("Automatic DB backup failed")
        return None
