"""
API 生命周期状态机 (v2.0+)
==========================

状态：
    draft    草稿     —— 可编辑、可删除、不可被网关调用
    pending  待上线   —— 审批中，不可编辑、不可删除、不可被调用
    online   已上线   —— 只读，不可编辑、不可删除，仅此状态可被网关调用
    offline  下线     —— 语义同草稿（可再编辑/提交），不可被调用

合法转换（动作 -> 新状态）：
    submit   提交上线：draft/offline -> pending
    approve  审批通过：pending        -> online
    reject   审批驳回：pending        -> draft
    offline  下线    ：online         -> offline
    withdraw 撤回    ：pending         -> draft（提交人撤回审批）
"""

STATUS_DRAFT = "draft"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"   # 审核通过待上线（满足审批通过条件，等提交者点上线）
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"

ALL_STATUSES = (STATUS_DRAFT, STATUS_PENDING, STATUS_APPROVED, STATUS_ONLINE, STATUS_OFFLINE)

STATUS_LABELS = {
    STATUS_DRAFT: "草稿",
    STATUS_PENDING: "审核中",
    STATUS_APPROVED: "待上线",
    STATUS_ONLINE: "已上线",
    STATUS_OFFLINE: "已下线",
}

# 动作 -> (允许的当前状态集合, 目标状态)
_TRANSITIONS = {
    "submit":   ({STATUS_DRAFT, STATUS_OFFLINE}, STATUS_PENDING),
    "approve":  ({STATUS_PENDING}, STATUS_APPROVED),   # 审批通过 -> 待上线（不自动 online）
    "reject":   ({STATUS_PENDING}, STATUS_DRAFT),
    "withdraw": ({STATUS_PENDING, STATUS_APPROVED}, STATUS_DRAFT),
    "publish":  ({STATUS_APPROVED}, STATUS_ONLINE),    # 提交者确认上线
    "offline":  ({STATUS_ONLINE}, STATUS_OFFLINE),
}


def can_edit(status: str) -> bool:
    """是否允许编辑该状态的 API。仅 draft / offline 可编辑。"""
    return status in (STATUS_DRAFT, STATUS_OFFLINE)


def can_delete(status: str) -> bool:
    """是否允许删除。已上线(online)与待上线(pending)不可删除。"""
    return status in (STATUS_DRAFT, STATUS_OFFLINE)


def can_be_called(status: str) -> bool:
    """是否可被网关对外调用。仅 online。"""
    return status == STATUS_ONLINE


def can_transition(action: str, current: str) -> bool:
    rule = _TRANSITIONS.get(action)
    if not rule:
        return False
    allowed, _ = rule
    return current in allowed


def next_status(action: str, current: str) -> str:
    """返回执行动作后的新状态；非法转换抛 ValueError。"""
    rule = _TRANSITIONS.get(action)
    if not rule:
        raise ValueError(f"未知动作: {action}")
    allowed, target = rule
    if current not in allowed:
        raise ValueError(
            f"当前状态「{STATUS_LABELS.get(current, current)}」不允许执行「{action}」"
        )
    return target
