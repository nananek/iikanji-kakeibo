// Phase E3-F PR-D-3: 医療費一覧 (medical/index.html) のクライアント描画。
//
// MK 復号 → fetch (journals + medical_expenses) → merge → DOM 構築。詳細編集
// モーダルも復号済データから直接埋め、保存は medical_expense_builder で暗号化
// して POST /api/v1/medical-expenses に送る。サーバ側は平文を一切読まない。
//
// 監査代理閲覧時はオーナーの MK 暗号値をユーザーの MK では復号できないため、
// 早期 return + status info で空表示の旨を案内 (medical_summary_renderer と同じ)。


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


const _PROVIDER_BADGE = {
  hospital: { cls: "bg-primary", label: "病院" },
  pharmacy: { cls: "bg-success", label: "薬局" },
  nursing: { cls: "bg-warning text-dark", label: "介護" },
  other: { cls: "bg-secondary", label: "その他" },
};


/**
 * 復号済の journals と medical_expenses をマージして一覧行を生成。
 *
 * 医療費科目 (tax_category="medical") への借方明細 1 つ = 1 行。entry_id と
 * medical_expense_id を保持するのが medical_summary_renderer.mergeExpenses との
 * 違い (詳細編集モーダルが必要とするため)。日付降順。
 *
 * @param {Array<Object>} journals  fetchJournalsForYear の戻り値
 * @param {Array<Object>} mexpenses fetchMedicalExpensesForYear の戻り値
 * @param {Object} accountsMeta     code → {name, tax_category}
 * @returns {Array<Object>}
 */
export function buildMedicalRows(journals, mexpenses, accountsMeta) {
  const medicalCodes = new Set();
  for (const [code, meta] of Object.entries(accountsMeta || {})) {
    if (meta && meta.tax_category === "medical") medicalCodes.add(code);
  }
  const meByEntry = new Map();
  for (const me of mexpenses || []) {
    if (me.journal_entry_id != null) meByEntry.set(me.journal_entry_id, me);
  }

  const rows = [];
  for (const entry of journals || []) {
    for (const line of entry.lines || []) {
      if (!medicalCodes.has(line.account_code)) continue;
      if (!((line.debit || 0) > 0)) continue;
      const me = meByEntry.get(entry.id) || null;
      const meta = accountsMeta[line.account_code];
      rows.push({
        entry_id: entry.id,
        medical_expense_id: me ? me.id : null,
        date: entry.date || (me && me.date) || null,
        description: entry.description || "",
        account_name: meta ? meta.name : line.account_code,
        amount: line.debit,
        patient_name: (me && me.patient_name) || "",
        hospital_name: (me && me.hospital_name) || "",
        treatment_description: (me && me.treatment_description) || "",
        provider_type: (me && me.provider_type) || "",
        insurance_reimbursement: (me && me.insurance_reimbursement) || 0,
      });
    }
  }
  rows.sort((a, b) => {
    const ad = a.date || "";
    const bd = b.date || "";
    if (ad === bd) return 0;
    return ad < bd ? 1 : -1; // 降順
  });
  return rows;
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("medical-index-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("medical-index-status");
  if (el) el.classList.add("d-none");
}


// モーダル / 保存で再利用する module 状態。
let _rowsByEntry = new Map();
let _userId = null;
let _bsModal = null;


function _getModal() {
  if (!_bsModal && globalThis.bootstrap) {
    _bsModal = new globalThis.bootstrap.Modal(
      document.getElementById("medicalModal"),
    );
  }
  return _bsModal;
}


function _renderTotals(rows) {
  let paid = 0;
  let reimbursed = 0;
  for (const r of rows) {
    paid += r.amount || 0;
    reimbursed += r.insurance_reimbursement || 0;
  }
  const paidEl = document.getElementById("medical-total-paid");
  const reimbEl = document.getElementById("medical-total-reimbursed");
  const netEl = document.getElementById("medical-total-net");
  if (paidEl) paidEl.textContent = _fmtYen(paid);
  if (reimbEl) reimbEl.textContent = _fmtYen(reimbursed);
  if (netEl) netEl.textContent = _fmtYen(paid - reimbursed);
}


function _providerBadge(pt) {
  const def = _PROVIDER_BADGE[pt];
  if (!def) return null;
  const span = document.createElement("span");
  span.className = "badge " + def.cls;
  span.textContent = def.label;
  return span;
}


function _renderRows(rows) {
  const tbody = document.getElementById("medical-index-tbody");
  const tableWrap = document.getElementById("medical-index-table");
  const empty = document.getElementById("medical-index-empty");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

  if (tableWrap) tableWrap.classList.toggle("d-none", rows.length === 0);
  if (empty) empty.classList.toggle("d-none", rows.length !== 0);

  for (const r of rows) {
    const tr = document.createElement("tr");

    function _td(text, cls) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = text;
      tr.appendChild(td);
      return td;
    }

    _td(_fmtDateYMD(r.date));
    _td(r.description);
    _td(r.patient_name);
    _td(r.hospital_name);

    const typeTd = document.createElement("td");
    const badge = _providerBadge(r.provider_type);
    if (badge) typeTd.appendChild(badge);
    tr.appendChild(typeTd);

    _td(r.treatment_description, "text-muted small");
    _td(_fmtYen(r.amount), "text-end");
    _td(r.insurance_reimbursement ? _fmtYen(r.insurance_reimbursement) : "", "text-end");
    _td(_fmtYen((r.amount || 0) - (r.insurance_reimbursement || 0)), "text-end");

    const actionTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-outline-secondary btn-sm";
    btn.title = "詳細編集";
    btn.innerHTML = '<i class="bi bi-pencil"></i>';
    btn.addEventListener("click", () => openMedicalModal(r.entry_id));
    actionTd.appendChild(btn);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
}


function _renderSuggestions(rows) {
  const patients = new Set();
  const hospitals = new Set();
  for (const r of rows) {
    if (r.patient_name) patients.add(r.patient_name);
    if (r.hospital_name) hospitals.add(r.hospital_name);
  }
  _fillDatalist("patientList", patients);
  _fillDatalist("hospitalList", hospitals);
}


function _fillDatalist(id, values) {
  const dl = document.getElementById(id);
  if (!dl) return;
  while (dl.firstChild) dl.removeChild(dl.firstChild);
  for (const v of [...values].sort()) {
    const opt = document.createElement("option");
    opt.value = v;
    dl.appendChild(opt);
  }
}


// --- 詳細編集モーダル ---


function openMedicalModal(entryId) {
  const row = _rowsByEntry.get(entryId);
  if (!row) return;
  const errEl = document.getElementById("medicalModalError");
  if (errEl) errEl.classList.add("d-none");
  document.getElementById("medModalEntryId").value = String(entryId);
  document.getElementById("medPatient").value = row.patient_name || "";
  document.getElementById("medHospital").value = row.hospital_name || "";
  document.getElementById("medProviderType").value = row.provider_type || "";
  document.getElementById("medReimbursement").value =
    row.insurance_reimbursement || 0;
  document.getElementById("medDescription").value =
    row.treatment_description || "";
  const modal = _getModal();
  if (modal) modal.show();
}


async function saveMedical() {
  const errEl = document.getElementById("medicalModalError");
  if (errEl) errEl.classList.add("d-none");
  const entryId = parseInt(document.getElementById("medModalEntryId").value, 10);
  const row = _rowsByEntry.get(entryId);
  if (!row) return;

  const spinner = document.getElementById("medSpinner");
  const saveBtn = document.getElementById("medSaveBtn");
  if (spinner) spinner.classList.remove("d-none");
  if (saveBtn) saveBtn.disabled = true;

  const [{ SharedCryptoClient }, { buildMedicalExpense }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/shared-client.js"),
    import(getStaticRoot() + "js/crypto/medical_expense_builder.js"),
  ]);

  let client;
  try {
    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      throw new Error("暗号鍵 (MK) がロックされています (設定 → 暗号鍵管理 で解除)。");
    }
    const payload = await buildMedicalExpense({
      client,
      userId: _userId,
      journalEntryId: entryId,
      date: row.date,
      patientName: document.getElementById("medPatient").value,
      hospitalName: document.getElementById("medHospital").value,
      treatmentDescription: document.getElementById("medDescription").value,
      providerType: document.getElementById("medProviderType").value,
      amountPaid: row.amount || 0,
      insuranceReimbursement:
        parseInt(document.getElementById("medReimbursement").value, 10) || 0,
    });
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";
    const res = await fetch("/api/v1/medical-expenses", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(payload),
    });
    const rb = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(rb.error || "HTTP " + res.status);
    }
    try {
      sessionStorage.setItem("flash:success", "医療費を更新しました。");
    } catch (_e) { /* ignore */ }
    globalThis.location.reload();
  } catch (e) {
    if (spinner) spinner.classList.add("d-none");
    if (saveBtn) saveBtn.disabled = false;
    if (errEl) {
      errEl.textContent = "保存に失敗しました: " + (e.message || e);
      errEl.classList.remove("d-none");
    }
  } finally {
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


async function _run() {
  const paramsEl = document.getElementById("medical-index-params");
  if (!paramsEl) return;
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (_e) {
    _setStatus("医療費一覧の初期化に失敗しました。", "danger");
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number") {
    _setStatus("医療費一覧の初期化に失敗しました (params)。", "danger");
    return;
  }
  _userId = params.user_id;

  let accountsMeta = {};
  const accountsEl = document.getElementById("medical-index-accounts-meta");
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (_e) {
    _setStatus("医療費一覧の初期化に失敗しました (accounts)。", "danger");
    return;
  }

  const saveBtn = document.getElementById("medSaveBtn");
  if (saveBtn) saveBtn.addEventListener("click", saveMedical);

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { fetchMedicalExpensesForYear },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/medical_expenses_client.js"),
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
        "監査代理閲覧中です。オーナーの暗号化された医療費データはあなたの暗号鍵では復号できないため、一覧は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      _renderRows([]);
      _renderTotals([]);
      return;
    }
    _clearStatus();

    const [journals, mexpenses] = await Promise.all([
      fetchJournalsForYear({
        client, userId: params.user_id, fiscalYear: params.year,
      }),
      fetchMedicalExpensesForYear({
        client, userId: params.user_id, fiscalYear: params.year,
      }),
    ]);
    const rows = buildMedicalRows(journals, mexpenses, accountsMeta);
    _rowsByEntry = new Map(rows.map((r) => [r.entry_id, r]));
    _renderTotals(rows);
    _renderRows(rows);
    _renderSuggestions(rows);
  } catch (e) {
    _setStatus("医療費一覧の取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


// window へ公開 (インラインの onclick からも参照可能にする保険)。
if (typeof window !== "undefined") {
  window.openMedicalModal = openMedicalModal;
  window.saveMedical = saveMedical;
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
