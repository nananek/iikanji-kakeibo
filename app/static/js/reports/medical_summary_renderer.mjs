// 医療費控除サマリのクライアント描画。
// MK 復号 → fetch (journals + medical_expenses) → merge → compute → DOM 構築。
//
// 監査代理閲覧時はオーナーの MK 暗号値をユーザーの MK では復号できないため、
// 早期 return + status info で空表示の旨を案内。


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


function _fmtDateYMD(iso) {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? m[1] + "/" + m[2] + "/" + m[3] : iso;
}


function _providerLabel(pt) {
  if (pt === "hospital") return "病院";
  if (pt === "pharmacy") return "薬局";
  if (pt === "nursing") return "介護";
  if (pt === "other") return "その他";
  return "";
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("medical-summary-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("medical-summary-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("medical_summary_renderer: " + msg, logArg);
  _setStatus("医療費集計の初期化に失敗しました: " + msg, "danger");
}


function _renderTotals(view) {
  const paidEl = document.getElementById("medical-total-paid");
  const reimbEl = document.getElementById("medical-total-reimbursed");
  const netEl = document.getElementById("medical-total-net");
  if (paidEl) paidEl.textContent = _fmtYen(view.totals.paid);
  if (reimbEl) reimbEl.textContent = _fmtYen(view.totals.reimbursed);
  if (netEl) netEl.textContent = _fmtYen(view.totals.net);
}


function _renderCsvLink(hasExpenses) {
  // CSV ダウンロード href は server-side で year 入りで生成済み。
  // ここでは表示/非表示を切り替えるだけ。
  const el = document.getElementById("medical-csv-link");
  if (!el) return;
  el.classList.toggle("d-none", !hasExpenses);
}


function _renderPatientCard(patient) {
  const card = document.createElement("div");
  card.className = "card shadow-sm mb-3";
  card.setAttribute("data-medical-patient", patient.name);

  const header = document.createElement("div");
  header.className = "card-header d-flex justify-content-between";
  const left = document.createElement("strong");
  const icon = document.createElement("i");
  icon.className = "bi bi-person";
  left.appendChild(icon);
  left.appendChild(document.createTextNode(" " + patient.name));
  header.appendChild(left);
  const right = document.createElement("span");
  const pieces = [];
  pieces.push("支払 " + _fmtYen(patient.paid));
  if (patient.reimbursed) {
    pieces.push("補填 " + _fmtYen(patient.reimbursed));
  }
  // 自己負担を strong で
  right.appendChild(document.createTextNode(pieces.join(" / ") + " / 自己負担 "));
  const netStrong = document.createElement("strong");
  netStrong.textContent = _fmtYen(patient.net);
  right.appendChild(netStrong);
  header.appendChild(right);
  card.appendChild(header);

  const body = document.createElement("div");
  body.className = "card-body p-0";
  const table = document.createElement("table");
  table.className = "table table-sm mb-0";

  const thead = document.createElement("thead");
  thead.className = "table-light";
  const trh = document.createElement("tr");
  for (const t of ["医療機関", "区分", "支払額", "補填額", "自己負担"]) {
    const th = document.createElement("th");
    if (t === "支払額" || t === "補填額" || t === "自己負担") {
      th.className = "text-end";
    }
    th.textContent = t;
    trh.appendChild(th);
  }
  thead.appendChild(trh);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const h of patient.hospitals || []) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    nameTd.textContent = h.name;
    tr.appendChild(nameTd);

    const typeTd = document.createElement("td");
    typeTd.textContent = _providerLabel(h.provider_type);
    tr.appendChild(typeTd);

    const paidTd = document.createElement("td");
    paidTd.className = "text-end";
    paidTd.textContent = _fmtYen(h.paid);
    tr.appendChild(paidTd);

    const reimbTd = document.createElement("td");
    reimbTd.className = "text-end";
    reimbTd.textContent = h.reimbursed ? _fmtYen(h.reimbursed) : "";
    tr.appendChild(reimbTd);

    const netTd = document.createElement("td");
    netTd.className = "text-end";
    netTd.textContent = _fmtYen(h.net);
    tr.appendChild(netTd);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  body.appendChild(table);
  card.appendChild(body);
  return card;
}


function _renderByPatient(view) {
  const wrap = document.getElementById("medical-by-patient");
  if (!wrap) return;
  while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
  for (const p of view.by_patient) {
    wrap.appendChild(_renderPatientCard(p));
  }
}


function _renderExpensesList(view) {
  const details = document.getElementById("medical-expenses-details");
  const tbody = document.getElementById("medical-expenses-tbody");
  const countEl = document.getElementById("medical-expenses-count");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  const list = view.expenses_list || [];
  if (countEl) countEl.textContent = String(list.length);
  if (details) details.classList.toggle("d-none", list.length === 0);
  for (const e of list) {
    const tr = document.createElement("tr");
    const cells = [
      _fmtDateYMD(e.date),
      e.description || "",
      e.patient_name || "",
      e.hospital_name || "",
      _providerLabel(e.provider_type),
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    const amountTd = document.createElement("td");
    amountTd.className = "text-end";
    amountTd.textContent = _fmtYen(e.amount);
    tr.appendChild(amountTd);
    const reimbTd = document.createElement("td");
    reimbTd.className = "text-end";
    reimbTd.textContent = e.insurance_reimbursement
      ? _fmtYen(e.insurance_reimbursement) : "";
    tr.appendChild(reimbTd);
    tbody.appendChild(tr);
  }
}


function _renderView(view) {
  _renderTotals(view);
  _renderCsvLink(view.expenses_list.length > 0);
  _renderByPatient(view);
  _renderExpensesList(view);
}


/**
 * journals entries と medical_expenses を merge して
 * computeMedicalSummary に渡せる形に。
 *
 * - journal_entry_id が entryMap にあれば entry.description / date と、
 *   entry 内の medical 科目 line の debit_amount + account_name を採用
 * - 同 entry に medical 科目 line が複数ある場合は **先頭の 1 件のみ採用**
 *   (現実には 1 entry 1 medical line が大半。複数 medical line の按分は
 *    follow-up #221 系で扱う)
 * - entry が見つからない、または medical line がない場合は
 *   me.amount_paid (medical_expense 復号値) を fallback
 *
 * export しているのは renderer の外からユニットテストするため (E3-F-3g
 * レビュー指摘対応)。
 */
export function mergeExpenses(entries, mexpenses, accountsMeta) {
  // medical 科目コードの set
  const medicalCodes = new Set();
  for (const [code, meta] of Object.entries(accountsMeta || {})) {
    if (meta && meta.tax_category === "medical") medicalCodes.add(code);
  }
  // entry_id → entry
  const entryMap = new Map();
  for (const e of entries) entryMap.set(e.id, e);
  // medical_expense 配列を merge
  const result = [];
  for (const me of mexpenses) {
    const eid = me.journal_entry_id;
    const entry = eid != null ? entryMap.get(eid) : null;
    // 該当 entry の medical 科目 line から amount 取得
    let amount = me.amount_paid || 0;
    let accountName = "";
    if (entry) {
      for (const l of entry.lines || []) {
        if (medicalCodes.has(l.account_code) && (l.debit || 0) > 0) {
          amount = l.debit;
          const am = accountsMeta[l.account_code];
          accountName = am ? am.name : l.account_code;
          break;
        }
      }
    }
    result.push({
      date: entry?.date || me.date || null,
      description: entry?.description || "",
      amount,
      account_name: accountName,
      patient_name: me.patient_name || "",
      hospital_name: me.hospital_name || "",
      treatment_description: me.treatment_description || "",
      provider_type: me.provider_type || "",
      insurance_reimbursement: me.insurance_reimbursement || 0,
    });
  }
  return result;
}


async function _run() {
  const paramsEl = document.getElementById("medical-summary-server-params");
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

  const accountsEl = document.getElementById("medical-summary-accounts-meta");
  let accountsMeta = {};
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    _abort("failed to parse accounts meta", e);
    return;
  }

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { fetchMedicalExpensesForYear },
      { computeMedicalSummary },
      { composeMedicalSummaryView },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/medical_expenses_client.js"),
      import(getStaticRoot() + "js/crypto/reports/medical_summary.js"),
      import(getStaticRoot() + "js/crypto/reports/medical_summary_view.js"),
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
        "監査代理閲覧中です。オーナーの暗号化された医療費データはあなたの暗号鍵では復号できないため、集計は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    const [entries, mexpenses] = await Promise.all([
      fetchJournalsForYear({
        client, userId: params.user_id, fiscalYear: params.year,
      }),
      fetchMedicalExpensesForYear({
        client, userId: params.user_id, fiscalYear: params.year,
      }),
    ]);
    const merged = mergeExpenses(entries, mexpenses, accountsMeta);
    const result = computeMedicalSummary(merged);
    const view = composeMedicalSummaryView(result, merged);
    _renderView(view);
  } catch (e) {
    _setStatus("医療費集計の取得に失敗しました: " + (e.message || e), "danger");
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
