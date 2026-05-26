// 月次比較 (monthly comparison) のクライアント描画。
// MK 復号→集計→DOM 構築 + Chart.js 描画まで完結する。
//
// projection (当月着地予想) は当面サーバ側計算結果を JSON でクライアントが
// そのまま表示する (clientside 化は後続 PR)。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _fmtYen(n) {
  return "¥" + (n || 0).toLocaleString();
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("monthly-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("monthly-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("monthly_renderer: " + msg, logArg);
  _setStatus("月次比較の初期化に失敗しました: " + msg, "danger");
}


function _td(text, opts = {}) {
  const td = document.createElement("td");
  if (opts.className) td.className = opts.className;
  if (opts.colSpan) td.colSpan = opts.colSpan;
  if (opts.style) td.setAttribute("style", opts.style);
  if (opts.bold) {
    const strong = document.createElement("strong");
    strong.textContent = text;
    td.appendChild(strong);
  } else {
    td.textContent = text;
  }
  return td;
}


function _monthCellText(value, monthIdx1, currentMonth) {
  if (value !== 0) return _fmtYen(value);
  if (currentMonth && monthIdx1 > currentMonth) return " ";  // 未来月は空白
  return "0";
}


function _renderRow(opts) {
  // opts: { code, name, costType, months, total, isIncome, currentMonth }
  const tr = document.createElement("tr");
  if (opts.code) {
    tr.setAttribute("data-monthly-row", opts.code);
    tr.setAttribute("data-monthly-row-type", opts.isIncome ? "income" : "expense");
  }
  if (opts.costType === "fixed") tr.className = "table-info";
  else if (opts.costType === "occasional") tr.className = "table-light";

  // 科目名
  const nameTd = _td(opts.name, { className: "sticky-col" });
  tr.appendChild(nameTd);

  // 区分バッジ
  const badgeTd = document.createElement("td");
  badgeTd.className = "d-mobile-none";
  const badge = document.createElement("span");
  if (opts.costType === "fixed") {
    badge.className = "badge bg-info text-dark";
    badge.textContent = "固定";
  } else if (opts.costType === "variable") {
    badge.className = "badge bg-warning text-dark";
    badge.textContent = "変動";
  } else {
    badge.className = "badge bg-secondary";
    badge.textContent = opts.isIncome ? "臨時" : "随時";
  }
  badgeTd.appendChild(badge);
  tr.appendChild(badgeTd);

  // 12 ヶ月
  for (let i = 0; i < 12; i++) {
    const m = i + 1;
    const cls = "text-end"
      + (opts.currentMonth && m === opts.currentMonth ? " table-warning" : "");
    tr.appendChild(_td(
      _monthCellText(opts.months[i] || 0, m, opts.currentMonth),
      { className: cls },
    ));
  }

  // 合計
  tr.appendChild(_td(_fmtYen(opts.total), { className: "text-end fw-bold" }));

  // 月平均 (0 以外の月数で割る)
  const monthsWithData = opts.months.filter((v) => v > 0).length;
  const avg = monthsWithData > 0
    ? Math.round(opts.total / monthsWithData) : 0;
  tr.appendChild(_td(_fmtYen(avg), { className: "text-end" }));

  return tr;
}


function _renderSectionHeader(label) {
  const tr = document.createElement("tr");
  tr.className = "table-secondary";
  const td = _td(label, {
    className: "sticky-col", bold: true,
    style: "background:#e2e3e5 !important;",
  });
  tr.appendChild(td);
  // 残りのセル (区分 + 12 + 合計 + 平均)
  for (let i = 0; i < 15; i++) tr.appendChild(document.createElement("td"));
  return tr;
}


function _renderBizRow(bizMonthly, currentMonth, taxFormUrl) {
  const tr = document.createElement("tr");
  tr.className = "table-success fw-bold";

  const nameTd = document.createElement("td");
  nameTd.className = "sticky-col";
  nameTd.setAttribute("style", "background:#d1e7dd !important;");
  nameTd.appendChild(document.createTextNode("📁 事業所得"));
  if (taxFormUrl) {
    nameTd.appendChild(document.createTextNode(" "));
    const a = document.createElement("a");
    a.href = taxFormUrl;
    a.className = "ms-1 small text-decoration-none";
    a.textContent = "(内訳)";
    nameTd.appendChild(a);
  }
  tr.appendChild(nameTd);

  tr.appendChild(_td("", { className: "d-mobile-none" }));

  for (let i = 0; i < 12; i++) {
    const cls = "text-end"
      + (currentMonth && i + 1 === currentMonth ? " table-warning" : "");
    tr.appendChild(_td(
      _monthCellText(bizMonthly.months[i] || 0, i + 1, currentMonth),
      { className: cls },
    ));
  }
  tr.appendChild(_td(_fmtYen(bizMonthly.total), { className: "text-end" }));

  const monthsWithData = bizMonthly.months.filter((v) => v !== 0).length;
  const avg = monthsWithData > 0
    ? Math.round(bizMonthly.total / monthsWithData) : 0;
  tr.appendChild(_td(_fmtYen(avg), { className: "text-end" }));
  return tr;
}


function _renderTotalRow(label, totals, currentMonth) {
  const tr = document.createElement("tr");
  tr.className = "table-dark";
  tr.appendChild(_td(label, { className: "sticky-col-head", bold: true }));
  tr.appendChild(document.createElement("td"));
  for (let i = 0; i < 12; i++) {
    const m = i + 1;
    const cls = "text-end"
      + (currentMonth && m === currentMonth ? " table-warning" : "");
    tr.appendChild(_td(
      _monthCellText(totals[i] || 0, m, currentMonth),
      { className: cls, bold: true },
    ));
  }
  const total = totals.reduce((s, v) => s + v, 0);
  tr.appendChild(_td(_fmtYen(total), { className: "text-end", bold: true }));
  const monthsWithData = totals.filter((v) => v > 0).length;
  const avg = monthsWithData > 0
    ? Math.round(total / monthsWithData) : 0;
  tr.appendChild(_td(_fmtYen(avg), { className: "text-end", bold: true }));
  return tr;
}


function _renderBalanceRow(view, currentMonth) {
  const tr = document.createElement("tr");
  tr.className = "table-light fw-bold";
  const nameTd = _td("収支差額", {
    className: "sticky-col", bold: true, style: "background:#f8f9fa;",
  });
  tr.appendChild(nameTd);
  tr.appendChild(document.createElement("td"));
  for (let i = 0; i < 12; i++) {
    const bal = view.net_totals[i] || 0;
    const has = view.income_totals[i] || view.expense_totals[i];
    let cls = "text-end";
    if (bal < 0) cls += " text-danger";
    else if (bal > 0) cls += " text-success";
    if (currentMonth && i + 1 === currentMonth) cls += " table-warning";
    let text;
    if (has) text = _fmtYen(bal);
    else if (currentMonth && i + 1 > currentMonth) text = " ";
    else text = "0";
    tr.appendChild(_td(text, { className: cls }));
  }
  const totalBal = view.net_totals.reduce((s, v) => s + v, 0);
  let footCls = "text-end";
  if (totalBal < 0) footCls += " text-danger";
  else if (totalBal > 0) footCls += " text-success";
  tr.appendChild(_td(_fmtYen(totalBal), { className: footCls }));
  tr.appendChild(document.createElement("td"));
  return tr;
}


function _renderTable(view, params) {
  const tbody = document.getElementById("monthly-tbody");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  const cm = params.current_month || null;

  if (view.biz_monthly) {
    tbody.appendChild(_renderBizRow(view.biz_monthly, cm, params.tax_form_url));
  }

  tbody.appendChild(_renderSectionHeader("収入"));
  for (const a of view.income_accounts) {
    tbody.appendChild(_renderRow({
      code: a.code, name: a.name, costType: a.cost_type,
      months: a.months, total: a.total, isIncome: true, currentMonth: cm,
    }));
  }
  tbody.appendChild(_renderTotalRow("収入合計", view.income_totals, cm));

  tbody.appendChild(_renderSectionHeader("支出"));
  for (const a of view.expense_accounts) {
    tbody.appendChild(_renderRow({
      code: a.code, name: a.name, costType: a.cost_type,
      months: a.months, total: a.total, isIncome: false, currentMonth: cm,
    }));
  }
  tbody.appendChild(_renderTotalRow("支出合計", view.expense_totals, cm));

  tbody.appendChild(_renderBalanceRow(view, cm));
}


function _renderBreakdownCard(side, bd) {
  const totals = side === "income" ? bd.income_totals : bd.expense_totals;
  const monthly = side === "income" ? bd.income_monthly : bd.expense_monthly;
  const grand = side === "income" ? bd.income_grand : bd.expense_grand;
  const wrap = document.getElementById("monthly-" + side + "-breakdown");
  if (!wrap) return;
  if (grand <= 0) {
    wrap.classList.add("d-none");
    return;
  }
  wrap.classList.remove("d-none");
  // 数値テキスト
  const fixed = totals.fixed || 0;
  const variable = totals.variable || 0;
  const occasional = totals.occasional || 0;
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  const pct = (v) => grand > 0 ? (v / grand * 100).toFixed(1) + "%" : "0%";
  setText("monthly-" + side + "-fixed", _fmtYen(fixed));
  setText("monthly-" + side + "-fixed-pct", pct(fixed));
  setText("monthly-" + side + "-variable", _fmtYen(variable));
  setText("monthly-" + side + "-variable-pct", pct(variable));
  setText("monthly-" + side + "-occasional", _fmtYen(occasional));
  setText("monthly-" + side + "-occasional-pct", pct(occasional));
  setText("monthly-" + side + "-grand", _fmtYen(grand));

  // Chart.js が読まれていれば描画
  if (typeof globalThis.Chart === "undefined") return;
  const doughnutId = side === "income" ? "incomeTypeChart" : "costTypeChart";
  const trendId = side === "income" ? "incomeTypeTrendChart" : "costTypeTrendChart";
  const doughnutEl = document.getElementById(doughnutId);
  const trendEl = document.getElementById(trendId);
  const monthLabels = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"];
  const colors = side === "income"
    ? ["#0d6efd", "#198754", "#6c757d"]
    : ["#0dcaf0", "#ffc107", "#6c757d"];
  const trendBg = side === "income"
    ? ["rgba(13, 110, 253, 0.7)", "rgba(25, 135, 84, 0.7)", "rgba(108, 117, 125, 0.7)"]
    : ["rgba(13, 202, 240, 0.7)", "rgba(255, 193, 7, 0.7)", "rgba(108, 117, 125, 0.7)"];
  const labels3 = side === "income"
    ? ["固定収入", "変動収入", "臨時収入"]
    : ["固定費", "変動費", "随時費"];
  if (doughnutEl) {
    new globalThis.Chart(doughnutEl, {
      type: "doughnut",
      data: {
        labels: labels3,
        datasets: [{ data: [fixed, variable, occasional], backgroundColor: colors }],
      },
      options: { responsive: true, plugins: { tooltip: {
        callbacks: { label: (c) => c.label + ": ¥" + c.parsed.toLocaleString() },
      } } },
    });
  }
  if (trendEl) {
    new globalThis.Chart(trendEl, {
      type: "bar",
      data: {
        labels: monthLabels,
        datasets: [
          { label: labels3[0], data: monthly.fixed, backgroundColor: trendBg[0] },
          { label: labels3[1], data: monthly.variable, backgroundColor: trendBg[1] },
          { label: labels3[2], data: monthly.occasional, backgroundColor: trendBg[2] },
        ],
      },
      options: {
        responsive: true,
        scales: {
          x: { stacked: true },
          y: { stacked: true, beginAtZero: true, ticks: {
            callback: (v) => "¥" + v.toLocaleString(),
          } },
        },
        plugins: { tooltip: {
          callbacks: { label: (c) => c.dataset.label + ": ¥" + c.parsed.y.toLocaleString() },
        } },
      },
    });
  }
}


function _renderView(view, params) {
  _renderTable(view, params);
  _renderBreakdownCard("income", view.breakdown);
  _renderBreakdownCard("expense", view.breakdown);
}


async function _run() {
  const paramsEl = document.getElementById("monthly-server-params");
  if (!paramsEl) { _abort("server params script not found"); return; }
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    _abort("failed to parse params", e);
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number") {
    _abort("invalid params (user_id/year)");
    return;
  }

  const accountsEl = document.getElementById("monthly-accounts-meta");
  if (!accountsEl) { _abort("accounts meta script not found"); return; }
  let accountsMeta;
  try {
    accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    _abort("failed to parse accounts meta", e);
    return;
  }

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { computeMonthlyComparison },
      { composeMonthlyComparisonView },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/monthly_comparison.js"),
      import(getStaticRoot() + "js/crypto/reports/monthly_comparison_view.js"),
    ]);

    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      _setStatus(
        "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
        "warning",
      );
      return;
    }
    if (params.is_audit_proxy) {
      _setStatus(
        "監査代理閲覧中です。オーナーの暗号化された仕訳はあなたの暗号鍵では復号できないため、月次比較は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    const accountTypeByCode = {};
    const accountNameByCode = {};
    for (const [code, meta] of Object.entries(accountsMeta)) {
      accountTypeByCode[code] = meta.type;
      accountNameByCode[code] = meta.name;
    }

    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeMonthlyComparison(entries, {
      accountTypeByCode, accountNameByCode,
    });
    const view = composeMonthlyComparisonView(jsResult, accountsMeta);
    _renderView(view, params);
  } catch (e) {
    _setStatus("月次比較の取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
