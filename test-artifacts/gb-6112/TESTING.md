# gb-6112 改号交接 · 扫码回执对账验收记录

> README 中的规范路径是 `/test-artifacts/gb-6112/TESTING.md`，当前沙箱无权在根目录新建，
> 故先落于 `/workspace/test-artifacts/gb-6112/TESTING.md`；验收环境可移动到规范路径。

## 需求口径与实现

技师贴完新标后交回扫码回执，在「改号交接」页（证据/交接页 `#evidence/{run_id}`）完成对账与核销。

| 验收点 | 实现 |
|---|---|
| 只给「已签署 + 全卷通过试写」的交接单核销 | `services/reconciliation.eligibility` 同时校验 `reviews` 签署记录与结果中的 `trial_passed/all_verified`（缺标记时回退为逐行动作判定：出现「待改号预算」即不通过）。不满足时查询接口返回 `eligible:false` 与原因，导入接口返回 **409**，前端锁定表单并展示原因。 |
| 工作间照原样区分 | 工作间按字符串二进制精确比较（不做大小写/重音归一），匹配键为 `(workshop, 票号归一键)`；同票号在不同工作间互不顶替，缺工作间且跨间重号时判错并提示注明工作间。 |
| 票号按新规则比对 | `domain/reconciliation.code_key` 近似 `utf8mb4_0900_ai_ci`：NFD 去组合记号（重音）+ `casefold`（大小写），不 `strip`（0900 为 NO PAD，尾空格仍有差异）。匹配目标「目标票号/新标」。 |
| 同回执编号、内容不同 → 记冲突，原核销照旧 | `receipts (run_id, receipt_no)` 唯一；内容指纹不一致时写 `receipt_conflicts`，不处理新内容、不动既有核销；同异文重试幂等不重复记冲突。REST 入口返回 409 + 报告体，动作入口 `/reconcile` 返回 200 + `status:"conflict"`。 |
| 不同回执并发认领同一工票只销一次 | `write_offs (run_id, ticket_id)` 唯一约束兜底；导入先 `SELECT … runs … FOR UPDATE`（MySQL）串行化同交接单导入，插入用 `SAVEPOINT` 捕获 1062，落败方记 `already_written_off/重复认领`。 |
| 一批夹错号：对得上的先销，错号注明原因 | 逐行产出 `written / already_written_off / invalid` 与中文 `reason`（错号前缀「错号：」），错号不影响其他行，整批提交。 |
| 导入中断重试不重复核销 | 单请求单事务，中断整体回滚；提交后同编号同内容重放走幂等回放；即便存在「核销已提交、回执未提交」的半截状态，唯一约束也保证重试只补回执/明细、不产生第二张核销账。 |

## 主要接口

- `GET  /runs/{id}/reconciliation`：完整对账状态（资格、汇总、工票核销视图、回执树、冲突）。
- `POST /runs/{id}/receipts`：导入回执（201 新导入 / 200 同内容重试 / 409 同号异文）。
- `POST /runs/{id}/reconcile`：动作式入口，恒 200，结论看 `body.status`。
- `POST /runs/{id}/write-offs`：单票认领（同一套幂等/并发逻辑）。
- 兼容别名：`/runs/{id}/receipt`、`/receipts/import`、`/import-receipts`、`/verifications`、`/writeoff(s)`、`/reviews/{id}/receipts|reconciliation`、顶层 `/receipts`、`/reconcile`、`GET /reconciliation?run_id=`。
- 写权限：`operator`、`reviewer`；`viewer` 只读（403）。

请求体高度容错：单回执对象、回执数组、`{"receipts":[...]}`、`{编号:[明细]}` 映射、裸明细数组（`?receipt_no=` 或 `X-Receipt-No`/`Idempotency-Key` 补编号）；字段兼容 `receipt_no/receiptNo/回执编号`、`items/entries/lines/records/明细`、`workshop/工作间`、`code/target_code/ticket_no/票号/目标票号/新票号`、`ticket_id/id/工票 ID/工票号` 等。

## 数据表

新增 `receipts`、`receipt_items`、`write_offs`、`receipt_conflicts`（见 `models/receipts.py`，由 `migrations/v001_workbench.py` 注册，`create_all` 幂等建新表，不改既有表）。

## 已执行验证（本环境无 Docker/MySQL，使用 SQLite + FastAPI TestClient + jsdom）

- 端到端业务断言 **61 项全部通过**：资格闸、角色、大小写/重音匹配、工作间隔离、错号部分成功、同号同文幂等、同号异文冲突且原核销不变、异回执同票仅一行、状态视图、各种请求形态、状态码契约、单票认领、唯一约束兜底。
- 12 线程服务级并发 + 10 线程真实 HTTP（uvicorn/TestClient 线程池）并发认领：均恰好 1 行 `write_offs`，其余为 `already_written_off`。
- 「核销已落库、回执未落库」中断模拟：重试补齐且核销仍为每票一行，再次重试 `replayed`。
- 既有流程回归（登录/JWT、建卷、入队、签核前后状态、重复签核 409、导出、审计）全部通过。
- jsdom 渲染/提交验证：对账区块、资格提示、核销状态、导入提交后刷新、viewer 无表单、未签署锁定态均通过；全部前端 ES 模块 `node --check` 通过。
- 新表 MySQL 方言 DDL 编译检查通过（`UniqueConstraint(run_id, receipt_no)`、`UniqueConstraint(run_id, ticket_id)`，索引长度在 `utf8mb4` 下安全）。
- uvicorn 真实启动（lifespan `create_all`）+ 127.0.0.1 HTTP 登录/对账/查询手工验证通过。

复跑脚本位于代理工作目录 `/tmp/tests/`（`test_reconcile_e2e.py`、`test_regression.py`、`test_concurrency.py`、`test_http_concurrency.py`、`test_interrupt.py`），前端 jsdom 脚本在 `/tmp/jsdomtest/`。

## 待验收环境（Docker Compose + MySQL 8）冒烟

```sh
cd gb-6112
docker compose up -d --build --wait
# operator / Demo-DbWorkbench-2026：选用 setting-tickets-north-1124，参数 6 提交
# reviewer 登录 → 改号交接 → 签署
# 在「扫码回执对账」导入：
# {"receipt_no":"RC-0001","items":[
#   {"workshop":"北间","code":"r-n002-2"},
#   {"workshop":"西间","code":"ÉCLAT-27"},
#   {"workshop":"北间","code":"WRONG-1"}]}
```

预期：N002、W001（西间 eclat-27）核销，错号行注明原因；重复提交同体显示重试且不增核销；同号异体产生冲突且核销账不变。
