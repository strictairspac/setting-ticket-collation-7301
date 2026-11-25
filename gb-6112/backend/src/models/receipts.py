from sqlalchemy import (
    JSON, BigInteger, Column, ForeignKey, Integer, String, Table, UniqueConstraint,
)

from .base import metadata

# 扫码回执：receipt_no 在同一张交接单（run）内唯一，是导入重试的幂等键。
receipts = Table(
    "receipts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("receipt_no", String(80), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("content", JSON(none_as_null=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("run_id", "receipt_no", name="uq_receipts_run_no"),
)

# 回执逐行对账结果，重试同一回执时原样返回。
receipt_items = Table(
    "receipt_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("receipt_id", ForeignKey("receipts.id"), nullable=False, index=True),
    Column("receipt_no", String(80), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("ticket_id", String(16), nullable=True, index=True),
    Column("workshop", String(64), nullable=True),
    Column("code", String(120), nullable=True),
    Column("matched_code", String(120), nullable=True),
    Column("status", String(24), nullable=False),
    Column("reason", String(400), nullable=True),
    Column("created_at", BigInteger, nullable=False),
)

# 核销账：每张交接单的每张工票至多一行，数据库唯一约束兜底并发认领。
write_offs = Table(
    "write_offs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("ticket_id", String(16), nullable=False),
    Column("workshop", String(64), nullable=False),
    Column("code", String(120), nullable=False),
    Column("receipt_no", String(80), nullable=False),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("run_id", "ticket_id", name="uq_write_offs_run_ticket"),
)

# 同编号不同内容的冲突记录；冲突不影响既有核销。
receipt_conflicts = Table(
    "receipt_conflicts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("receipt_no", String(80), nullable=False),
    Column("reason", String(400), nullable=False),
    Column("existing_hash", String(64), nullable=True),
    Column("incoming_hash", String(64), nullable=True),
    Column("content", JSON(none_as_null=True), nullable=True),
    Column("actor", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
