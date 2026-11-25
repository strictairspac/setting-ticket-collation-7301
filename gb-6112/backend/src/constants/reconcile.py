"""扫码回执对账相关状态。"""
from enum import StrEnum


class ReceiptStatus(StrEnum):
    PROCESSED = "processed"
    CONFLICT = "conflict"


class ReceiptItemStatus(StrEnum):
    # 本次导入新核销
    WRITTEN = "written"
    # 工票此前已被（本回执或其他回执）核销，不重复销账
    ALREADY = "already_written_off"
    # 错号 / 对不上交接单
    INVALID = "invalid"


STATUS_TEXT = {
    ReceiptItemStatus.WRITTEN: "已核销",
    ReceiptItemStatus.ALREADY: "重复认领",
    ReceiptItemStatus.INVALID: "错号",
}

TICKET_WRITTEN_TEXT = "已核销"
TICKET_PENDING_TEXT = "未核销"
