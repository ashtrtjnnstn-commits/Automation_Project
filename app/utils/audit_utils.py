from __future__ import annotations

import logging
from datetime import datetime

from app.models import AuditLog, db

logger = logging.getLogger(__name__)


def log_audit(action: str, object_type: str, object_id: int | None = None, details: str = "") -> None:
    """Best-effort audit logging that must not break core workflows."""
    try:
        values = {
            "action": action,
            "entity_type": object_type,
            "entity_id": object_id,
            "details": details,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        with db.engine.begin() as conn:
            conn.execute(db.insert(AuditLog).values(**values))
    except Exception:
        logger.exception("Audit logging failed: action=%s object_type=%s object_id=%s", action, object_type, object_id)
