// 試算表 (残高試算表) のクライアント描画。
// MK 復号→集計→DOM 構築まで完結する (browser-only)。
//
// 監査代理閲覧時は API が effective user (= owner) を解決するが、
// 監査者の MK でオーナーの暗号化 entries は復号できないため、空表示になる。
// (Lv1 は API 自体が 403。Lv2/Lv3 は復号失敗 → 空)


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
  return "¥" + n.toLocaleString();
}


function _setStatusMessage(msg, type = "info") {
  const el = document.getElementById("trial-balance-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("trial-balance-status");
  if (el) el.classList.add("d-none");
}


function _renderSection(section) {
  const frag = document.createDocumentFragment();
  // 区分ヘッダー
  const header = document.createElement("tr");
  header.className = "table-secondary";
  const th = document.createElement("td");
  th.colSpan = 5;
  th.innerHTML = "<strong>" + section.typeName + "</strong>";
  header.appendChild(th);
  frag.appendChild(header);

  for (const row of section.rows) {
    const tr = document.createElement("tr");
    tr.setAttribute("data-trial-balance-row", row.code);
    tr.innerHTML = (
      '<td class="d-mobile-none">' + row.code + "</td>" +
      "<td>" + row.name + "</td>" +
      '<td class="text-end">' + (row.debit ? _fmtYen(row.debit) : "") + "</td>" +
      '<td class="text-end">' + (row.credit ? _fmtYen(row.credit) : "") + "</td>" +
      '<td class="text-end ' + (row.balance < 0 ? "text-danger" : "") + '">' +
        _fmtYen(row.balance) + "</td>"
    );
    frag.appendChild(tr);
  }

  // 小計
  const sub = document.createElement("tr");
  sub.className = "table-info";
  sub.innerHTML = (
    '<td class="d-mobile-none"></td>' +
    '<td class="text-end"><strong>' + section.typeName + " 小計</strong></td>" +
    '<td class="text-end"><strong>' + _fmtYen(section.subtotal.debit) + "</strong></td>" +
    '<td class="text-end"><strong>' + _fmtYen(section.subtotal.credit) + "</strong></td>" +
    '<td class="text-end"><strong>' + _fmtYen(section.subtotal.balance) + "</strong></td>"
  );
  frag.appendChild(sub);
  return frag;
}


function _renderView(view) {
  const tbody = document.getElementById("trial-balance-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (view.sections.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = (
      '<td colspan="5" class="text-center text-muted py-4">' +
      "表示できる仕訳がありません</td>"
    );
    tbody.appendChild(tr);
    return;
  }
  for (const section of view.sections) {
    tbody.appendChild(_renderSection(section));
  }
}


async function _run() {
  const paramsEl = document.getElementById("trial-balance-server-params");
  if (!paramsEl) return;
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("trial_balance_renderer: failed to parse params", e);
    return;
  }
  if (typeof params.user_id !== "number") return;
  if (typeof params.fiscal_year !== "number") return;

  const accountsEl = document.getElementById("trial-balance-accounts-meta");
  if (!accountsEl) return;
  let accountsMeta;
  try {
    accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    console.warn("trial_balance_renderer: failed to parse accounts meta", e);
    return;
  }

  const [
    { SharedCryptoClient },
    { fetchJournalsForYear },
    { computeTrialBalance },
    { composeTrialBalanceView },
  ] = await Promise.all([
    import(getStaticRoot() + "js/crypto/shared-client.js"),
    import(getStaticRoot() + "js/crypto/journals_client.js"),
    import(getStaticRoot() + "js/crypto/reports/trial_balance.js"),
    import(getStaticRoot() + "js/crypto/reports/trial_balance_view.js"),
  ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      _setStatusMessage(
        "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
        "warning",
      );
      return;
    }
    _clearStatus();
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.fiscal_year,
    });
    const jsRows = computeTrialBalance(entries, {
      fiscalPeriodFrom: params.fiscal_period_from,
      fiscalPeriodTo: params.fiscal_period_to,
    });
    const view = composeTrialBalanceView(jsRows, accountsMeta);
    _renderView(view);
  } catch (e) {
    _setStatusMessage("試算表の取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
