"""扫码回执与改号交接单的对账口径。

- 工作间沿用源卷的二进制排序规则（utf8mb4_0900_bin），原样精确区分；
- 票号切换到目标规则 utf8mb4_0900_ai_ci：大小写、重音不敏感；
  0900 排序规则为 NO PAD，因此首尾空格不去除、参与比较。

本模块只做纯计算，不访问数据库，便于单测覆盖。
"""
import json
import unicodedata
from dataclasses import dataclass

# 试写通过后，计划行里可能出现的动作。
VERIFIED_ACTIONS = {"保留", "别名已验", "试写通过", "verified", "kept", "alias_verified"}
PENDING_ACTIONS = {"待改号预算", "pending_budget", "pending"}

RECEIPT_NO_KEYS = (
    "receipt_no", "receiptNo", "receipt_number", "receiptNumber",
    "receipt_id", "receiptId", "serial_no", "serialNo",
    "no", "number", "code", "id",
    "回执编号", "回执号", "编号", "扫码回执编号", "回执单号",
)
# 明确表示“回执编号”的键；裸 id/code/no 可能是条目字段，只在信封形态下采信。
RECEIPT_NO_STRONG_KEYS = (
    "receipt_no", "receiptNo", "receipt_number", "receiptNumber",
    "receipt_id", "receiptId", "serial_no", "serialNo",
    "回执编号", "回执号", "编号", "扫码回执编号", "回执单号",
)
RECEIPT_NO_WEAK_KEYS = ("no", "number", "id", "code")
RECEIPT_LIST_KEYS = (
    "receipts", "batches", "records", "list", "data",
    "回执", "回执列表", "回执单", "批次",
)
ITEM_LIST_KEYS = (
    "items", "entries", "lines", "details", "receipt_items", "receiptItems",
    "scans", "scan_records", "tickets", "codes",
    "明细", "回执明细", "项目", "条目", "扫码记录", "扫码明细",
)
TICKET_ID_KEYS = (
    "ticket_id", "ticketId", "id", "ticket",
    "工票ID", "工票 Id", "工票id", "工票编号", "工票号", "工票",
)
WORKSHOP_KEYS = (
    "workshop", "workshop_name", "workshopName", "room", "shop",
    "工作间", "车间", "工坊",
)
CODE_KEYS = (
    "target_code", "targetCode", "new_code", "newCode", "label_code", "labelCode",
    "scanned_code", "scannedCode", "scan_code", "code", "ticket_code", "ticketCode",
    "ticket_no", "ticketNo", "ticket_number", "ticketNumber",
    "new_ticket_no", "target_ticket_no",
    "number", "no", "label", "qr", "value", "text", "scan", "scanned",
    "目标票号", "新票号", "票号", "扫码票号", "扫码内容", "标签号", "编号",
)
RUN_ID_KEYS = ("run_id", "runId", "run", "batch_id", "batchId", "交接单", "批次编号", "批次")


@dataclass(frozen=True)
class HandoverTicket:
    ticket_id: str
    workshop: str
    original_code: str
    target_code: str
    action: str


@dataclass(frozen=True)
class ReceiptItemInput:
    line_no: int
    ticket_id: str | None
    workshop: str | None
    code: str | None
    raw: object


@dataclass(frozen=True)
class ReceiptInput:
    receipt_no: str
    items: list[ReceiptItemInput]


def code_key(code) -> str:
    """近似 utf8mb4_0900_ai_ci 的比较键。

    NFD 分解后去掉组合记号（重音），再 casefold（大小写）。
    不做 strip：目标排序规则是 NO PAD。
    """
    if code is None:
        return ""
    text = unicodedata.normalize("NFD", str(code))
    # 去掉组合记号（重音）：combining class 非 0 的字符丢弃，其余保留。
    text = "".join(ch for ch in text if unicodedata.combining(ch) == 0)
    return unicodedata.normalize("NFC", text).casefold()


def workshop_key(workshop) -> str:
    """工作间按二进制规则比较：不做大小写/重音归一，仅统一成字符串。"""
    return "" if workshop is None else str(workshop)


def _first_key(item: dict, keys):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _as_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return value
    return str(value)


def parse_item(raw, line_no: int) -> ReceiptItemInput:
    if isinstance(raw, str):
        text = raw.strip()
        # 兼容扫码枪直接读出 JSON / "工作间/票号" 之类的复合内容。
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    return parse_item(loaded, line_no)
            except (ValueError, TypeError):
                pass
        if "/" in text and not text.startswith("http"):
            head, _, tail = text.partition("/")
            if head and tail:
                return ReceiptItemInput(line_no, None, head.strip() or None, tail.strip() or None, raw)
        return ReceiptItemInput(line_no, None, None, raw, raw)
    if not isinstance(raw, dict):
        return ReceiptItemInput(line_no, None, None, None, raw)
    ticket_id = _as_str(_first_key(raw, TICKET_ID_KEYS))
    workshop = _as_str(_first_key(raw, WORKSHOP_KEYS))
    code = _as_str(_first_key(raw, CODE_KEYS))
    return ReceiptItemInput(line_no, ticket_id or None, workshop or None, code or None, raw)


def _has_item_list(raw: dict) -> bool:
    value = _first_key(raw, ITEM_LIST_KEYS)
    return isinstance(value, list)


def _looks_like_receipt(raw: dict) -> bool:
    if _first_key(raw, RECEIPT_NO_STRONG_KEYS) is not None:
        return True
    if _has_item_list(raw):
        return True
    return False


def _coerce_receipt(raw, fallback_no: str | None) -> ReceiptInput:
    if isinstance(raw, list):
        receipt_no = fallback_no
        raw_items = raw
    elif isinstance(raw, dict):
        strong_no = _as_str(_first_key(raw, RECEIPT_NO_STRONG_KEYS))
        envelope = _has_item_list(raw)
        receipt_no = strong_no or fallback_no
        if not receipt_no and envelope:
            # 只有在确实是回执信封（带明细数组）时，才把弱编号键当作回执编号。
            receipt_no = _as_str(_first_key(raw, RECEIPT_NO_WEAK_KEYS))
        raw_items = _first_key(raw, ITEM_LIST_KEYS)
        if raw_items is None:
            # 单个条目形态：{"receipt_no": "R1", "workshop": "北间", "code": "..."}
            if _first_key(raw, CODE_KEYS) is not None or _first_key(raw, TICKET_ID_KEYS) is not None:
                raw_items = [raw]
            else:
                raw_items = []
        if isinstance(raw_items, str):
            raw_items = [line for line in raw_items.splitlines() if line.strip()]
    else:
        raise ValueError("回执内容应为 JSON 对象或数组")
    if not receipt_no or not str(receipt_no).strip():
        raise ValueError("回执缺少回执编号（receipt_no / 回执编号）")
    receipt_no = str(receipt_no).strip()
    if len(receipt_no) > 80:
        raise ValueError("回执编号不能超过 80 个字符")
    if not isinstance(raw_items, list):
        raise ValueError("回执明细应为数组（items / 明细）")
    items = [parse_item(entry, index + 1) for index, entry in enumerate(raw_items)]
    if not items:
        raise ValueError(f"回执 {receipt_no} 没有扫码明细")
    if len(items) > 1000:
        raise ValueError("单张回执明细不能超过 1000 行")
    return ReceiptInput(receipt_no, items)


def parse_submission(payload, fallback_no: str | None = None) -> list[ReceiptInput]:
    """把各种常见提交形态归一成回执列表。

    支持：单回执对象、回执数组、{"receipts": [...]}、{回执编号: [条目...]} 映射等。
    """
    if payload is None:
        raise ValueError("请提交扫码回执 JSON")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValueError("回执内容不是合法 JSON") from exc

    raw_receipts = None
    if isinstance(payload, list):
        # 数组既可能是多张回执，也可能是一张回执的明细。
        if payload and all(isinstance(entry, dict) and _looks_like_receipt(entry)
                           for entry in payload):
            raw_receipts = payload
        else:
            raw_receipts = [payload]
    elif isinstance(payload, dict):
        listed = _first_key(payload, RECEIPT_LIST_KEYS)
        if isinstance(listed, list):
            raw_receipts = listed
        elif isinstance(listed, dict):
            raw_receipts = [listed]
        elif _first_key(payload, ITEM_LIST_KEYS) is not None or _first_key(payload, CODE_KEYS) is not None \
                or _first_key(payload, TICKET_ID_KEYS) is not None or _first_key(payload, RECEIPT_NO_KEYS) is not None:
            raw_receipts = [payload]
        else:
            # 映射形态：{"RC-1": [{...}], "RC-2": [{...}]}
            mapped = []
            for key, value in payload.items():
                if key in RUN_ID_KEYS:
                    continue
                if isinstance(value, list):
                    mapped.append({"receipt_no": key, "items": value})
            if mapped:
                raw_receipts = mapped
    if raw_receipts is None:
        raise ValueError("无法识别的回执格式：需要回执编号与扫码明细")
    return [_coerce_receipt(raw, fallback_no) for raw in raw_receipts]


def extract_run_id(payload):
    if isinstance(payload, dict):
        value = _first_key(payload, RUN_ID_KEYS)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _row_value(row: dict, keys, default=""):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def handover_tickets(run) -> list[HandoverTicket]:
    result = (run or {}).get("result") if isinstance(run, dict) else None
    rows = (result or {}).get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    tickets = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticket_id = _as_str(_row_value(row, ("工票 ID", "工票ID", "ticket_id", "ticketId", "id")))
        workshop = _as_str(_row_value(row, ("工作间", "workshop")))
        original = _as_str(_row_value(row, ("原票号", "original_code", "source_code", "code_before", "code")))
        target = _as_str(_row_value(row, (
            "目标票号", "target_code", "new_code", "proposed", "proposed_code", "目标",
        ), None)) or original
        action = _as_str(_row_value(row, ("动作", "action", "state", "操作"), ""))
        if not ticket_id:
            continue
        tickets.append(HandoverTicket(ticket_id, workshop or "", original or "", target or "", action or ""))
    return tickets


def trial_all_passed(run) -> bool:
    """全卷通过目标唯一约束试写：结果里没有“待改号预算”之类的未验行。"""
    if not isinstance(run, dict) or run.get("status") != "done" or not run.get("result"):
        return False
    result = run["result"]
    for key in ("trial_passed", "all_verified", "handover_ready", "全卷通过试写"):
        if key in result:
            return bool(result[key])
    tickets = handover_tickets(run)
    if not tickets:
        return False
    return all(ticket.action in VERIFIED_ACTIONS for ticket in tickets)


class HandoverIndex:
    def __init__(self, tickets: list[HandoverTicket]):
        self.tickets = tickets
        self.by_id = {ticket.ticket_id: ticket for ticket in tickets}
        self.by_target: dict[tuple[str, str], HandoverTicket] = {}
        for ticket in tickets:
            self.by_target.setdefault((workshop_key(ticket.workshop), code_key(ticket.target_code)), ticket)

    def _cross_workshop(self, code):
        key = code_key(code)
        return [candidate for (_ws, code_key_value), candidate in self.by_target.items()
                if code_key_value == key]

    def _by_code(self, code, workshop):
        key = code_key(code)
        if workshop:
            scoped = self.by_target.get((workshop_key(workshop), key))
            if scoped is not None:
                return scoped
            # 票号字段里填了工票 ID 的情况：工作间必须一致。
            as_id = self.by_id.get(str(code))
            if as_id is not None and workshop_key(workshop) == workshop_key(as_id.workshop):
                return as_id
            return None
        matches = self._cross_workshop(code)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "ambiguous"
        # 也兼容把工票 ID 填进票号字段的扫码内容。
        return self.by_id.get(str(code))

    def resolve(self, item: ReceiptItemInput):
        """返回 (ticket, reason)。ticket 为 None 时 reason 说明错号原因。"""
        # 1) 稳定工票 ID 直接命中。
        if item.ticket_id and item.ticket_id in self.by_id:
            ticket = self.by_id[item.ticket_id]
            if item.workshop and workshop_key(item.workshop) != workshop_key(ticket.workshop):
                return None, (
                    f"错号：工票 {item.ticket_id} 属于工作间「{ticket.workshop}」，"
                    f"回执写的是「{item.workshop}」，工作间须原样一致"
                )
            if item.code and code_key(item.code) != code_key(ticket.target_code):
                return None, (
                    f"错号：回执票号「{item.code}」与交接目标票号「{ticket.target_code}」"
                    f"（{ticket.workshop}）按新排序规则不一致"
                )
            return ticket, None

        # 2) 票号 + 工作间，按新排序规则匹配目标新标。
        if item.code:
            matched = self._by_code(item.code, item.workshop)
            if matched == "ambiguous":
                workshops = "、".join(sorted({
                    candidate.workshop for candidate in self._cross_workshop(item.code)}))
                return None, f"错号：票号「{item.code}」在多个工作间（{workshops}）重号，请注明工作间"
            if matched is not None:
                return matched, None
            if item.workshop:
                return None, (
                    f"错号：工作间「{item.workshop}」下没有按新排序规则匹配到票号「{item.code}」的目标新标"
                )
            return None, f"错号：交接单中找不到票号「{item.code}」"

        # 3) 只有一个标识字段：可能是 ID，也可能是被别名键收进来的票号。
        if item.ticket_id:
            matched = self._by_code(item.ticket_id, item.workshop)
            if matched == "ambiguous":
                return None, f"错号：标识「{item.ticket_id}」在多个工作间重号，请注明工作间"
            if matched is not None:
                return matched, None
            return None, f"错号：工票标识「{item.ticket_id}」不是本交接单的工票 ID，也匹配不到目标票号"

        return None, "错号：回执条目缺少票号或工票 ID"
