"""操作审计 (v2.2+)：记录敏感操作。"""
from app.models.models import AuditLog
from app.core.logging import get_logger

log = get_logger("audit")


async def audit(db, user, action: str, target_type: str = "", target_id=None, detail: str = ""):
    """写一条审计日志。db 为当前请求的 AsyncSession（调用方负责 commit）。"""
    try:
        db.add(AuditLog(
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", ""),
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail or "",
        ))
        log.info(f"[audit] {getattr(user,'username','?')} {action} {target_type}#{target_id} {detail}")
    except Exception as e:
        log.warning(f"审计写入失败 | action={action} | error={e}")
