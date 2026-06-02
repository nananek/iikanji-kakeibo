// 確定申告控除集計 (tax summary) のクライアント描画。
// MK 復号→集計→DOM 構築まで完結する。


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
  const el = document.getElementById("tax-summary-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("tax-summary-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("tax_summary_renderer: " + msg, logArg);
  _setStatus("確定申告集計の初期化に失敗しました: " + msg, "danger");
}


function _renderCategoryCard(section) {
  const card = document.createElement("div");
  card.className = "card shadow-sm mb-3";
  card.setAttribute("data-tax-category", section.code);

  const header = document.createElement("div");
  header.className = "card-header d-flex justify-content-between";
  const labelStrong = document.createElement("strong");
  labelStrong.textContent = section.label;
  header.appendChild(labelStrong);
  const totalBadge = document.createElement("span");
  totalBadge.className = "badge bg-dark fs-6";
  totalBadge.textContent = _fmtYen(section.total);
  header.appendChild(totalBadge);
  card.appendChild(header);

  const body = document.createElement("div");
  body.className = "card-body p-0";
  const table = document.createElement("table");
  table.className = "table table-sm mb-0";
  const tbody = document.createElement("tbody");
  for (const acc of section.accounts) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = acc.name;
    const amountTd = document.createElement("td");
    amountTd.className = "text-end";
    amountTd.textContent = _fmtYen(acc.amount);
    tr.appendChild(nameTd);
    tr.appendChild(amountTd);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  body.appendChild(table);
  card.appendChild(body);
  return card;
}


function _renderView(view) {
  const wrap = document.getElementById("tax-summary-cards");
  const emptyMsg = document.getElementById("tax-summary-empty");
  if (!wrap) return;
  while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
  if (view.sections.length === 0) {
    if (emptyMsg) emptyMsg.classList.remove("d-none");
    return;
  }
  if (emptyMsg) emptyMsg.classList.add("d-none");
  for (const section of view.sections) {
    wrap.appendChild(_renderCategoryCard(section));
  }
}


async function _run() {
  const paramsEl = document.getElementById("tax-summary-server-params");
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

  const accountsEl = document.getElementById("tax-summary-accounts-meta");
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
      { computeTaxSummary },
      { composeTaxSummaryView },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/tax_summary.js"),
      import(getStaticRoot() + "js/crypto/reports/tax_summary_view.js"),
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
    _clearStatus();

    const taxCategoryByCode = {};
    const accountNameByCode = {};
    for (const [code, meta] of Object.entries(accountsMeta)) {
      if (meta.tax_category) taxCategoryByCode[code] = meta.tax_category;
      accountNameByCode[code] = meta.name;
    }

    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeTaxSummary(entries, {
      taxCategoryByCode, accountNameByCode,
    });
    const view = composeTaxSummaryView(jsResult);
    _renderView(view);
  } catch (e) {
    _setStatus("確定申告集計の取得に失敗しました: " + (e.message || e), "danger");
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
