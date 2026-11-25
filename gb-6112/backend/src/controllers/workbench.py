from redis.exceptions import RedisError
from fastapi import HTTPException

from src.config.resources import cache, engine
from src.services import cases, reconciliation, runs, reviews


def new_case(data, user):
    with engine.begin() as db:
        return cases.create(db, data, user["username"])


def new_run(case_id, data, user):
    with engine.begin() as db:
        run = runs.enqueue(db, case_id, data.parameter, user["username"])
    try:
        cache.lpush("workbench:wake", str(run["id"]))
        cache.ltrim("workbench:wake", 0, 99)
    except RedisError:
        # The database holds the durable queue. The worker also polls it.
        pass
    return run


def new_review(run_id, data, user):
    with engine.begin() as db:
        return reviews.sign(db, run_id, data, user["username"])


def import_receipts(run_id, payload, user, fallback_no=None):
    try:
        with engine.begin() as db:
            return reconciliation.import_submission(
                db, run_id, payload, user["username"], fallback_no=fallback_no
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def reconciliation_state(run_id):
    with engine.connect() as db:
        return reconciliation.state(db, run_id)


_CLAIM_ID_KEYS = ("ticket_id", "ticketId", "id", "ticket", "工票ID", "工票 Id", "工票id", "工票号", "工票编号")
_CLAIM_NO_KEYS = ("receipt_no", "receiptNo", "receipt_id", "receiptId", "回执编号", "回执号", "编号")


def claim_writeoff(run_id, claim, user):
    """单票认领/核销：包装成只含一行的回执走同一套幂等与并发逻辑。"""
    ticket_id = None
    receipt_no = None
    if isinstance(claim, dict):
        ticket_id = next((str(claim[k]) for k in _CLAIM_ID_KEYS if claim.get(k) not in (None, "")), None)
        receipt_no = next((str(claim[k]) for k in _CLAIM_NO_KEYS if claim.get(k) not in (None, "")), None)
    fallback = receipt_no or (f"DIRECT-{ticket_id}" if ticket_id else None)
    return import_receipts(run_id, claim, user, fallback_no=fallback)
