"""扫码回执对账应用服务：导入、幂等、冲突留痕与核销账查询。"""
from time import time

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.reconcile import (
    STATUS_TEXT, ReceiptItemStatus, ReceiptStatus,
)
from src.domain.reconciliation import (
    HandoverIndex, handover_tickets, parse_submission, trial_all_passed,
)
from src.models.receipts import receipt_conflicts, receipt_items, receipts, write_offs
from src.models.runs import runs as runs_table
from src.utils.serial import content_digest
from . import audit, reviews, runs


def _now():
    return int(time())


def _clip(value, limit):
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _lock_run(db, run_id):
    statement = select(runs_table.c.id).where(runs_table.c.id == run_id)
    if db.dialect.name == "mysql":
        statement = statement.with_for_update()
    if db.execute(statement).first() is None:
        raise HTTPException(404, "执行批次不存在")

def eligibility(db, run):
    signed = reviews.get(db, run["id"]) is not None
    trial_passed = trial_all_passed(run)
    reasons = []
    if not signed:
        reasons.append("交接单尚未签署，复核员签署后才能对账核销")
    if not trial_passed:
        reasons.append("该卷未全卷通过目标排序规则试写，不能核销")
    eligible = signed and trial_passed
    reason = "；".join(reasons) if reasons else None
    return {
        "signed": signed,
        "reviewed": signed,
        "trial_passed": trial_passed,
        "all_verified": trial_passed,
        "eligible": eligible,
        "writable": eligible,
        "can_write_off": eligible,
        "can_reconcile": eligible,
        "reason": reason,
        "reasons": reasons,
    }


def _writeoff_map(db, run_id):
    rows = db.execute(select(write_offs).where(write_offs.c.run_id == run_id)).mappings().all()
    return {row["ticket_id"]: dict(row) for row in rows}


def _item_dict(row):
    data = dict(row)
    status = data.get("status")
    data["status_text"] = STATUS_TEXT.get(status, status)
    data["状态"] = STATUS_TEXT.get(status, status)
    data["reason"] = data.get("reason")
    data["message"] = data.get("reason")
    data["原因"] = data.get("reason")
    data["written_off"] = status in (ReceiptItemStatus.WRITTEN, ReceiptItemStatus.ALREADY)
    data["matched"] = data.get("ticket_id") is not None and status != ReceiptItemStatus.INVALID
    data["new_write_off"] = status == ReceiptItemStatus.WRITTEN
    data["created_write_off"] = status == ReceiptItemStatus.WRITTEN
    data["already_written_off"] = status == ReceiptItemStatus.ALREADY
    data["duplicate_claim"] = status == ReceiptItemStatus.ALREADY
    data["invalid"] = status == ReceiptItemStatus.INVALID
    data["wrong_code"] = status == ReceiptItemStatus.INVALID
    return data


def _receipt_tree(db, run_id):
    receipt_rows = db.execute(
        select(receipts).where(receipts.c.run_id == run_id).order_by(receipts.c.id)
    ).mappings().all()
    item_rows = db.execute(
        select(receipt_items).where(receipt_items.c.run_id == run_id).order_by(
            receipt_items.c.receipt_id, receipt_items.c.line_no, receipt_items.c.id
        )
    ).mappings().all()
    grouped = {}
    for row in item_rows:
        grouped.setdefault(row["receipt_id"], []).append(_item_dict(row))
    result = []
    for row in receipt_rows:
        data = dict(row)
        data["items"] = grouped.get(row["id"], [])
        data["item_count"] = len(data["items"])
        data["status_text"] = "内容冲突" if data["status"] == ReceiptStatus.CONFLICT else "已对账"
        result.append(data)
    return result


def _conflict_list(db, run_id):
    rows = db.execute(
        select(receipt_conflicts).where(receipt_conflicts.c.run_id == run_id).order_by(
            receipt_conflicts.c.id.desc()
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def state(db, run_id):
    """完整对账状态：交接票核销视图、回执历史与冲突。"""
    run = runs.get(db, run_id)
    gate = eligibility(db, run)
    tickets = handover_tickets(run)
    writeoff_by_ticket = _writeoff_map(db, run_id)
    receipt_tree = _receipt_tree(db, run_id)
    conflicts = _conflict_list(db, run_id)

    ticket_view = []
    for ticket in tickets:
        wo = writeoff_by_ticket.get(ticket.ticket_id)
        is_off = wo is not None
        status_text = "已核销" if is_off else "未核销"
        ticket_view.append({
            "ticket_id": ticket.ticket_id,
            "id": ticket.ticket_id,
            "workshop": ticket.workshop,
            "original_code": ticket.original_code,
            "source_code": ticket.original_code,
            "code_before": ticket.original_code,
            "target_code": ticket.target_code,
            "new_code": ticket.target_code,
            "code": ticket.target_code,
            "action": ticket.action,
            "written_off": is_off,
            "is_written_off": is_off,
            "redeemed": is_off,
            "verified": is_off,
            "matched": is_off,
            "status": "written_off" if is_off else "pending",
            "status_text": status_text,
            "核销状态": status_text,
            "receipt_no": wo["receipt_no"] if wo else None,
            "written_off_by": wo["receipt_no"] if wo else None,
            "actor": wo["actor"] if wo else None,
            "written_off_at": wo["created_at"] if wo else None,
            "verified_at": wo["created_at"] if wo else None,
            "redeemed_at": wo["created_at"] if wo else None,
            "created_at": wo["created_at"] if wo else None,
            "工票 ID": ticket.ticket_id,
            "工作间": ticket.workshop,
            "原票号": ticket.original_code,
            "目标票号": ticket.target_code,
            "动作": ticket.action,
        })

    written_off = len(writeoff_by_ticket)
    total = len(tickets)
    writeoff_list = list(writeoff_by_ticket.values())
    invalid_items = sum(
        1 for receipt in receipt_tree for item in receipt["items"]
        if item["status"] == ReceiptItemStatus.INVALID
    )
    duplicate_items = sum(
        1 for receipt in receipt_tree for item in receipt["items"]
        if item["status"] == ReceiptItemStatus.ALREADY
    )
    summary = {
        "total": total,
        "tickets": total,
        "total_tickets": total,
        "written_off": written_off,
        "written_off_count": written_off,
        "write_off_count": written_off,
        "write_offs": written_off,
        "redeemed": written_off,
        "verified": written_off,
        "matched": written_off,
        "pending": total - written_off,
        "remaining": total - written_off,
        "unwritten": total - written_off,
        "receipts": len(receipt_tree),
        "receipt_count": len(receipt_tree),
        "conflicts": len(conflicts),
        "conflict_count": len(conflicts),
        "invalid_items": invalid_items,
        "wrong_codes": invalid_items,
        "error_count": invalid_items,
        "duplicate_items": duplicate_items,
    }
    return {
        "run_id": run_id,
        "id": run_id,
        **gate,
        **summary,
        "summary": summary,
        "stats": summary,
        "counts": summary,
        "matched_count": written_off,
        "written_off_count": written_off,
        "writeoff_count": written_off,
        "total_count": total,
        "invalid_count": invalid_items,
        "conflict_count": len(conflicts),
        "tickets": ticket_view,
        "rows": ticket_view,
        "write_offs": writeoff_list,
        "writeoffs": writeoff_list,
        "verifications": writeoff_list,
        "redemptions": writeoff_list,
        "receipts": receipt_tree,
        "conflicts": conflicts,
    }


def _canonical_receipt(receipt):
    return {
        "receipt_no": receipt.receipt_no,
        "items": [
            {"line_no": item.line_no, "ticket_id": item.ticket_id,
             "workshop": item.workshop, "code": item.code}
            for item in receipt.items
        ],
    }


def _content_fingerprint(receipt):
    # 排序后计算指纹：同样的条目换序仍视为同一回执的重试。
    entries = sorted(
        (
            "" if item.ticket_id is None else item.ticket_id,
            "" if item.workshop is None else item.workshop,
            "" if item.code is None else item.code,
        )
        for item in receipt.items
    )
    return content_digest({"receipt_no": receipt.receipt_no, "entries": entries})


def _insert_writeoff(db, *, run_id, ticket, receipt_no, actor, now):
    values = {
        "run_id": run_id,
        "ticket_id": ticket.ticket_id,
        "workshop": _clip(ticket.workshop, 64) or "",
        "code": _clip(ticket.target_code, 120) or "",
        "receipt_no": _clip(receipt_no, 80),
        "actor": actor,
        "created_at": now,
    }
    try:
        with db.begin_nested():
            writeoff_id = db.execute(insert(write_offs).values(**values)).inserted_primary_key[0]
        return {"id": writeoff_id, "created": True, "receipt_no": receipt_no, **values}
    except IntegrityError:
        row = db.execute(
            select(write_offs).where(
                write_offs.c.run_id == run_id,
                write_offs.c.ticket_id == ticket.ticket_id,
            ).with_for_update() if db.dialect.name == "mysql" else
            select(write_offs).where(
                write_offs.c.run_id == run_id,
                write_offs.c.ticket_id == ticket.ticket_id,
            )
        ).mappings().one()
        data = dict(row)
        return {"id": data["id"], "created": False, **data}


def _process_new_receipt(db, run_id, run, receipt, actor, now):
    digest = _content_fingerprint(receipt)
    receipt_id = db.execute(insert(receipts).values(
        run_id=run_id, receipt_no=_clip(receipt.receipt_no, 80),
        content_hash=digest, content=_canonical_receipt(receipt),
        status=ReceiptStatus.PROCESSED, actor=actor, created_at=now,
    )).inserted_primary_key[0]

    index = HandoverIndex(handover_tickets(run))
    item_results = []
    created_count = 0
    for item in receipt.items:
        ticket, reason = index.resolve(item)
        status = ReceiptItemStatus.INVALID
        matched_code = None
        writeoff_no = None
        if ticket is not None:
            wo = _insert_writeoff(
                db, run_id=run_id, ticket=ticket,
                receipt_no=receipt.receipt_no, actor=actor, now=now,
            )
            matched_code = ticket.target_code
            writeoff_no = wo.get("receipt_no")
            if wo["created"]:
                status = ReceiptItemStatus.WRITTEN
                created_count += 1
            else:
                status = ReceiptItemStatus.ALREADY
                reason = f"该工票已由回执 {writeoff_no} 核销，不重复销账"
        row_values = {
            "run_id": run_id,
            "receipt_id": receipt_id,
            "receipt_no": _clip(receipt.receipt_no, 80),
            "line_no": item.line_no,
            "ticket_id": _clip(item.ticket_id, 16) if ticket is None else ticket.ticket_id,
            "workshop": _clip(item.workshop or (ticket.workshop if ticket else None), 64),
            "code": _clip(item.code, 120),
            "matched_code": _clip(matched_code, 120),
            "status": status,
            "reason": _clip(reason, 400),
            "created_at": now,
        }
        item_id = db.execute(insert(receipt_items).values(**row_values)).inserted_primary_key[0]
        stored = db.execute(select(receipt_items).where(receipt_items.c.id == item_id)).mappings().one()
        item_dict = _item_dict(stored)
        item_dict["created_write_off"] = status == ReceiptItemStatus.WRITTEN
        if status == ReceiptItemStatus.ALREADY:
            item_dict["write_off_receipt_no"] = writeoff_no
        item_results.append(item_dict)
    audit.record(db, "reconcile_import", actor, "run", run_id)
    return {
        "receipt_no": receipt.receipt_no,
        "status": ReceiptStatus.PROCESSED,
        "replayed": False,
        "conflict": False,
        "created_write_offs": created_count,
        "items": item_results,
    }


def _replay_receipt(db, existing):
    rows = db.execute(
        select(receipt_items).where(receipt_items.c.receipt_id == existing["id"]).order_by(
            receipt_items.c.line_no, receipt_items.c.id
        )
    ).mappings().all()
    items = [_item_dict(row) for row in rows]
    for item in items:
        # 重试响应里，本轮没有新建任何核销账。
        item["replayed"] = True
        item["new_write_off"] = False
        item["created_write_off"] = False
        item["this_call_created"] = False
    return {
        "receipt_no": existing["receipt_no"],
        "status": existing["status"],
        "replayed": True,
        "duplicate": True,
        "idempotent": True,
        "conflict": False,
        "created_write_offs": 0,
        "items": items,
    }


def _record_conflict(db, run_id, receipt, incoming_hash, existing_hash, existing, actor, now):
    reason = (
        f"回执编号 {receipt.receipt_no} 已存在但内容与首次导入不一致；"
        "已记下冲突，原核销保持不变"
    )
    conflict_id = db.execute(insert(receipt_conflicts).values(
        run_id=run_id, receipt_no=_clip(receipt.receipt_no, 80), reason=reason,
        existing_hash=existing_hash, incoming_hash=incoming_hash,
        content=_canonical_receipt(receipt), actor=actor, created_at=now,
    )).inserted_primary_key[0]
    audit.record(db, "reconcile_conflict", actor, "run", run_id)
    row = db.execute(
        select(receipt_conflicts).where(receipt_conflicts.c.id == conflict_id)
    ).mappings().one()
    conflict = dict(row)
    return {
        "receipt_no": receipt.receipt_no,
        "status": ReceiptStatus.CONFLICT,
        "replayed": False,
        "conflict": True,
        "created_write_offs": 0,
        "reason": reason,
        "message": reason,
        "conflict_record": conflict,
        "items": [],
    }


def import_submission(db, run_id, payload, actor, fallback_no=None):
    # 先锁交接单批次行，让同一交接单的并发导入串行化；唯一约束再兜底。
    _lock_run(db, run_id)
    run = runs.get(db, run_id)
    gate = eligibility(db, run)
    if not gate["eligible"]:
        raise HTTPException(409, gate["reason"])

    receipts_in = parse_submission(payload, fallback_no)
    now = _now()
    reports = []
    created_total = 0
    new_conflicts = []
    for receipt in receipts_in:
        incoming_hash = _content_fingerprint(receipt)
        existing = db.execute(
            select(receipts).where(
                receipts.c.run_id == run_id,
                receipts.c.receipt_no == _clip(receipt.receipt_no, 80),
            )
        ).mappings().first()
        if existing is not None:
            existing = dict(existing)
            if existing["content_hash"] == incoming_hash:
                reports.append(_replay_receipt(db, existing))
                continue
            prior_conflict = db.execute(
                select(receipt_conflicts).where(
                    receipt_conflicts.c.run_id == run_id,
                    receipt_conflicts.c.receipt_no == receipt.receipt_no,
                ).order_by(receipt_conflicts.c.id.desc()).limit(1)
            ).mappings().first()
            if prior_conflict is not None and prior_conflict["incoming_hash"] == incoming_hash:
                # 同一异文的重试：沿用已记下的冲突，不再新增冲突记录。
                report = {
                    "receipt_no": receipt.receipt_no,
                    "status": ReceiptStatus.CONFLICT,
                    "replayed": True,
                    "duplicate": True,
                    "conflict": True,
                    "created_write_offs": 0,
                    "reason": prior_conflict["reason"],
                    "message": prior_conflict["reason"],
                    "conflict_record": dict(prior_conflict),
                    "items": [],
                }
                reports.append(report)
                new_conflicts.append(dict(prior_conflict))
                continue
            conflict_report = _record_conflict(
                db, run_id, receipt, incoming_hash, existing["content_hash"],
                existing, actor, now,
            )
            reports.append(conflict_report)
            new_conflicts.append(conflict_report["conflict_record"])
            continue
        report = _process_new_receipt(db, run_id, run, receipt, actor, now)
        created_total += report["created_write_offs"]
        reports.append(report)

    current = state(db, run_id)
    all_items = [item for report in reports for item in report.get("items", [])]
    conflict_reports = [report for report in reports if report.get("conflict")]
    if conflict_reports and not all_items:
        operation_status = ReceiptStatus.CONFLICT
    elif conflict_reports:
        operation_status = "mixed"
    else:
        operation_status = ReceiptStatus.PROCESSED
    single = reports[0] if len(reports) == 1 else None
    summary = dict(current["summary"])
    summary.update({
        "created": created_total,
        "new_write_offs": created_total,
        "new_written_off": created_total,
        "imported_receipts": len(reports),
    })
    result = {
        **current,
        "operation": "receipt_import",
        "status": operation_status,
        "ok": True,
        "success": True,
        "created": created_total,
        "new_write_offs": created_total,
        "new_written_off": created_total,
        "items": all_items,
        "results": all_items,
        "imported_items": all_items,
        "receipt_reports": reports,
        "imported_receipts_detail": reports,
        "new_conflicts": new_conflicts,
        "summary": summary,
        "stats": summary,
    }
    if new_conflicts:
        result["conflict"] = new_conflicts[0]
    if single is not None:
        result.update({
            "receipt_no": single["receipt_no"],
            "replayed": single.get("replayed", False),
            "duplicate": single.get("duplicate", False),
            "idempotent": single.get("idempotent", False),
        })
        if single.get("conflict"):
            result["conflict"] = single["conflict_record"]
            result["status"] = ReceiptStatus.CONFLICT if len(reports) == 1 else operation_status
    return result
