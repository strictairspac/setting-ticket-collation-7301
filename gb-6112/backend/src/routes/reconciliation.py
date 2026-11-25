"""改号交接页的扫码回执对账接口。

主路径：
- GET  /runs/{run_id}/reconciliation   完整对账状态
- POST /runs/{run_id}/receipts         导入扫码回执（幂等；201 新导入 / 200 重试 / 409 内容冲突）
- POST /runs/{run_id}/reconcile        动作式对账入口，始终 200，结果看 body.status

同时保留一组常见命名的别名，方便不同调用约定接入。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from src.controllers.access import identity, staff
from src.controllers.workbench import claim_writeoff, import_receipts, reconciliation_state
from src.domain.reconciliation import extract_run_id

router = APIRouter(tags=["Reconciliation"])


async def _json_body(request: Request):
    try:
        return await request.json()
    except Exception as exc:  # noqa: BLE001 - 任何 JSON 解析失败都转成 422
        raise HTTPException(422, "请提交合法的 JSON 回执内容") from exc


def _fallback_no(request: Request, receipt_no: str | None):
    return (
        receipt_no
        or request.headers.get("x-receipt-no")
        or request.headers.get("idempotency-key")
        or request.query_params.get("no")
        or request.query_params.get("receiptNo")
    )


async def _run_import(run_id, request, user, receipt_no):
    payload = await _json_body(request)
    # 数据库事务在线程池里执行，保证并发请求是真正并发的事务。
    return await run_in_threadpool(
        import_receipts, run_id, payload, user, _fallback_no(request, receipt_no)
    )


def _rest(report):
    """资源式语义状态码：同号异文 409，同号同文重试 200，新导入 201。"""
    if report.get("status") == "conflict":
        return JSONResponse(status_code=409, content=report)
    if report.get("replayed"):
        return JSONResponse(status_code=200, content=report)
    return JSONResponse(status_code=201, content=report)


@router.get("/runs/{run_id}/reconciliation", dependencies=[Depends(identity)])
def get_reconciliation(run_id: int):
    return reconciliation_state(run_id)


@router.get("/runs/{run_id}/reconcile", dependencies=[Depends(identity)])
def get_reconcile_alias(run_id: int):
    return reconciliation_state(run_id)


@router.get("/runs/{run_id}/receipts", dependencies=[Depends(identity)])
def list_receipts(run_id: int):
    # 集合路径按 REST 习惯直接返回收据列表（含逐行结果）。
    return reconciliation_state(run_id)["receipts"]


@router.get("/runs/{run_id}/conflicts", dependencies=[Depends(identity)])
def list_conflicts(run_id: int):
    return reconciliation_state(run_id)["conflicts"]


@router.get("/runs/{run_id}/write-offs", dependencies=[Depends(identity)])
def list_write_offs(run_id: int):
    return reconciliation_state(run_id)["write_offs"]


@router.get("/runs/{run_id}/writeoffs", dependencies=[Depends(identity)])
def list_writeoffs_alias(run_id: int):
    return reconciliation_state(run_id)["write_offs"]


@router.get("/runs/{run_id}/verifications", dependencies=[Depends(identity)])
def list_verifications(run_id: int):
    return reconciliation_state(run_id)["verifications"]


@router.get("/reviews/{run_id}/receipts", dependencies=[Depends(identity)])
def list_review_receipts(run_id: int):
    return reconciliation_state(run_id)["receipts"]


@router.get("/reviews/{run_id}/reconciliation", dependencies=[Depends(identity)])
def get_review_reconciliation(run_id: int):
    return reconciliation_state(run_id)


# ---- 资源式导入：语义化 201/200/409 -------------------------------------

@router.post("/runs/{run_id}/receipts")
async def post_receipts(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return _rest(await _run_import(run_id, request, user, receipt_no))


@router.post("/runs/{run_id}/receipt")
async def post_receipt_singular(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return _rest(await _run_import(run_id, request, user, receipt_no))


async def _put_named_receipt(run_id, receipt_no, request, user):
    payload = await _json_body(request)
    # URL 中的回执编号作为权威幂等键：对象体若没带编号就用它。
    report = await run_in_threadpool(import_receipts, run_id, payload, user, receipt_no)
    return _rest(report)


@router.put("/runs/{run_id}/receipts/{receipt_no}")
async def put_named_receipts(
    run_id: int, receipt_no: str, request: Request, user=Depends(staff),
):
    return await _put_named_receipt(run_id, receipt_no, request, user)


@router.put("/runs/{run_id}/receipt/{receipt_no}")
async def put_named_receipt(
    run_id: int, receipt_no: str, request: Request, user=Depends(staff),
):
    return await _put_named_receipt(run_id, receipt_no, request, user)


@router.get("/runs/{run_id}/receipts/{receipt_no}", dependencies=[Depends(identity)])
def get_named_receipt(run_id: int, receipt_no: str):
    for receipt in reconciliation_state(run_id)["receipts"]:
        if receipt["receipt_no"] == receipt_no:
            return receipt
    raise HTTPException(404, "回执不存在")


@router.post("/runs/{run_id}/verifications")
async def post_verifications(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return _rest(await _run_import(run_id, request, user, receipt_no))


@router.post("/reviews/{run_id}/receipts")
async def post_review_receipts(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return _rest(await _run_import(run_id, request, user, receipt_no))


@router.post("/runs/{run_id}/receipts/import")
async def import_receipts_path(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return _rest(await _run_import(run_id, request, user, receipt_no))


@router.post("/runs/{run_id}/import-receipts")
async def import_receipts_hyphen(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return _rest(await _run_import(run_id, request, user, receipt_no))


# ---- 动作式对账：始终 200，冲突写在 body.status ---------------------------

def _claim_body(report):
    item = report.get("items")[0] if report.get("items") else None
    body = {
        **report,
        "ticket_id": (item or {}).get("ticket_id"),
        "written_off": bool((item or {}).get("written_off")),
        "created": bool((item or {}).get("new_write_off")),
        "already_written_off": bool((item or {}).get("already_written_off")),
        "status": (item or {}).get("status", report.get("status")),
        "reason": (item or {}).get("reason") or report.get("reason"),
    }
    return body


@router.post("/runs/{run_id}/write-offs")
async def claim_write_off(run_id: int, request: Request, user=Depends(staff)):
    claim = await _json_body(request)
    report = await run_in_threadpool(claim_writeoff, run_id, claim, user)
    body = _claim_body(report)
    if body["status"] == "already_written_off" or report.get("replayed"):
        return JSONResponse(status_code=200, content=body)
    return JSONResponse(status_code=201, content=body)


@router.post("/runs/{run_id}/writeoff")
async def claim_write_off_alias(run_id: int, request: Request, user=Depends(staff)):
    claim = await _json_body(request)
    report = await run_in_threadpool(claim_writeoff, run_id, claim, user)
    body = _claim_body(report)
    if body["status"] == "already_written_off" or report.get("replayed"):
        return JSONResponse(status_code=200, content=body)
    return JSONResponse(status_code=201, content=body)


@router.post("/runs/{run_id}/reconcile")
async def post_reconcile(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return await _run_import(run_id, request, user, receipt_no)


@router.post("/runs/{run_id}/reconciliation")
async def post_reconciliation(
    run_id: int, request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return await _run_import(run_id, request, user, receipt_no)


@router.get("/reconciliation", dependencies=[Depends(identity)])
def get_reconciliation_by_query(run_id: int):
    return reconciliation_state(run_id)


async def _import_top_level(request: Request, user, receipt_no, semantic: bool):
    payload = await _json_body(request)
    run_id = extract_run_id(payload)
    if run_id is None:
        raise HTTPException(422, "请求中缺少 run_id（交接单编号）")
    report = await run_in_threadpool(
        import_receipts, run_id, payload, user, _fallback_no(request, receipt_no)
    )
    return _rest(report) if semantic else report


@router.post("/receipts")
async def post_receipts_top_level(
    request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return await _import_top_level(request, user, receipt_no, True)


@router.post("/reconcile")
async def post_reconcile_top_level(
    request: Request, receipt_no: str | None = None, user=Depends(staff),
):
    return await _import_top_level(request, user, receipt_no, False)
