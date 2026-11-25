import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, section } from "../components/common/panel.js";
import { table } from "../components/common/table.js";
import { submit } from "../hooks/submit.js";
import { el } from "../utils/dom.js";
import { time } from "../utils/format.js";

const STATUS_LABEL = {
  written: "本次核销",
  already_written_off: "重复认领",
  invalid: "错号",
};

function badge(text, tone, testId) {
  return el("span", {class: `tag tag-${tone}`, "data-testid": testId || undefined}, text);
}

function ticketStatus(ticket) {
  if (ticket.written_off) {
    return badge("已核销", "ok", "writeoff-status");
  }
  return badge("未核销", "muted", "writeoff-status");
}

function itemStatus(item) {
  const label = item.status_text || STATUS_LABEL[item.status] || item.status;
  if (item.status === "written") return badge(label, "ok", "item-status");
  if (item.status === "already_written_off") return badge(label, "warn", "item-status");
  return badge(label, "bad", "item-status");
}

function metricsBar(summary) {
  const metrics = [
    ["工票总数", summary.total],
    ["已核销", summary.written_off],
    ["待核销", summary.pending],
    ["错号条目", summary.invalid_items],
    ["冲突", summary.conflicts],
  ];
  return el("dl", {class: "metrics reconcile-metrics"},
    metrics.map(([label, value]) => el("div", {}, el("dt", {}, label), el("dd", {}, String(value)))));
}

function ticketTable(tickets) {
  if (!tickets.length) return empty("交接单没有工票明细。");
  return table(
    ["工票 ID", "工作间", "目标票号", "核销状态", "核销回执", "核销时间"],
    tickets.map(ticket => [
      el("span", {"data-ticket": ticket.ticket_id}, ticket.ticket_id),
      ticket.workshop,
      ticket.target_code || "—",
      ticketStatus(ticket),
      ticket.receipt_no || "—",
      ticket.written_off_at ? time(ticket.written_off_at) : "—",
    ]),
    "工票核销状态",
  );
}

function reportTable(items) {
  if (!items?.length) return null;
  return table(
    ["行", "工作间", "扫码票号", "匹配工票", "结果", "原因"],
    items.map(item => [
      item.line_no,
      item.workshop || "—",
      item.code || "—",
      item.ticket_id || "—",
      itemStatus(item),
      item.reason || "—",
    ]),
    "本次对账明细",
  );
}

function conflictPanel(conflicts) {
  if (!conflicts?.length) return null;
  return el("div", {class: "conflict-list", "data-testid": "conflicts"},
    el("h3", {}, "回执冲突"),
    table(
      ["回执编号", "冲突原因", "记录时间"],
      conflicts.map(conflict => [
        el("span", {"data-conflict-no": conflict.receipt_no}, conflict.receipt_no),
        conflict.reason,
        time(conflict.created_at),
      ]),
      "回执编号冲突",
    ));
}

function receiptHistory(receipts) {
  if (!receipts?.length) return null;
  return el("details", {class: "receipt-history"},
    el("summary", {}, `已导入回执 ${receipts.length} 张`),
    receipts.map(receipt => el("div", {class: "receipt-record"},
      el("h4", {}, `${receipt.receipt_no} · ${receipt.status_text || receipt.status} · ${time(receipt.created_at)}`),
      reportTable(receipt.items) || empty("无明细。"))));
}

function importForm(runId, onImported, tickets) {
  const pending = (tickets || []).filter(ticket => !ticket.written_off);
  const noInput = el("input", {name: "receipt_no", maxlength: 80, placeholder: "例如 RC-20261125-01"});
  const payloadInput = el("textarea", {
    name: "payload", rows: 8, spellcheck: "false", required: true,
    placeholder: '{\n  "receipt_no": "RC-0001",\n  "items": [\n    {"workshop": "北间", "code": "r-n002-2"}\n  ]\n}',
  });
  const fillExample = el("button", {
    type: "button", class: "quiet",
    onclick: () => {
      const picks = (pending.length ? pending : tickets || []).slice(0, 3);
      noInput.value = noInput.value || `RC-DEMO-${String(Date.now()).slice(-6)}`;
      payloadInput.value = JSON.stringify({
        receipt_no: noInput.value,
        items: picks.map(ticket => ({workshop: ticket.workshop, code: ticket.target_code})),
      }, null, 2);
    },
  }, "填入示例回执");
  const fileInput = el("input", {
    type: "file", accept: ".json,application/json", "aria-label": "选择扫码回执 JSON 文件",
  });
  const issue = errorText();
  const form = el("form", {class: "reconcile-form", "data-testid": "receipt-import-form"},
    field("回执编号（JSON 未含编号时使用）", noInput, "同一编号重复提交按导入重试处理，不会重复核销。"),
    field("扫码回执内容（JSON）", payloadInput, "可贴单张回执、回执数组，或 {\"receipts\": [...]}；夹有错号不影响能对上的票。"),
    el("div", {class: "reconcile-tools"}, fillExample, field("从文件导入", fileInput)),
    issue,
    el("button", {type: "submit", "data-testid": "receipt-import-button"}, "提交并导入回执，开始对账并确认核销"));

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    payloadInput.value = await file.text();
    if (!noInput.value) {
      try {
        const parsed = JSON.parse(payloadInput.value);
        const candidate = parsed?.receipt_no || parsed?.receiptNo || parsed?.["回执编号"];
        if (candidate) noInput.value = candidate;
      } catch {
        // 留给提交时统一报错。
      }
    }
    if (payloadInput.value.trim()) form.requestSubmit();
  });

  submit(form, async values => {
    issue.textContent = "";
    let payload;
    try {
      payload = JSON.parse(values.get("payload"));
    } catch {
      throw new Error("回执 JSON 无法解析，请检查括号、引号和逗号");
    }
    const report = await api.importReceipts(runId, payload, values.get("receipt_no") || undefined);
    await onImported(report);
  });
  return form;
}

export async function reconcileSection(run, user) {
  let state = await api.reconciliation(run.id);
  const region = el("div", {class: "reconcile-body", "data-testid": "reconciliation"});

  async function refresh(report) {
    state = await api.reconciliation(run.id);
    render(report);
  }

  function render(lastReport) {
    const gate = state.eligible
      ? el("p", {class: "gate gate-ok", "data-testid": "reconcile-gate"},
          `交接单已签署，全卷 ${state.summary.total} 张工票通过目标规则试写，可对账核销。`)
      : el("p", {class: "gate gate-locked", role: "note", "data-testid": "reconcile-gate"},
          state.reason || "当前交接单还不能核销。");

    const reportBlock = lastReport
      ? el("div", {class: "import-report", "data-testid": "import-report"},
          el("h3", {}, lastReport.status === "conflict" ? "本次导入：编号冲突，原核销照旧"
            : `本次新核销 ${lastReport.created ?? lastReport.new_write_offs ?? 0} 张`),
          lastReport.replayed ? el("p", {class: "tag tag-warn"}, "相同回执重复提交，已按重试处理，未重复核销。") : null,
          lastReport.conflict?.reason ? el("p", {class: "feedback"}, lastReport.conflict.reason) : null,
          reportTable(lastReport.items))
      : null;

    const canWrite = state.eligible && user.role !== "viewer";
    region.replaceChildren(...[
      gate,
      metricsBar(state.summary),
      canWrite ? importForm(run.id, refresh, state.tickets)
        : state.eligible ? empty("只读账号可查看对账结果，核销导入由操作员或复核员执行。")
        : empty("满足“已签署且全卷通过试写”后才能导入回执核销。"),
      reportBlock,
      section("工票核销账", ticketTable(state.tickets)),
      conflictPanel(state.conflicts),
      receiptHistory(state.receipts),
    ].filter(Boolean));
  }

  render();
  return section("扫码回执对账", region);
}
