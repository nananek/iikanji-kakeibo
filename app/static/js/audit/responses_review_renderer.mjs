// owner 側の修正案 / 差戻しレビュー UI (E5 #112 / §14.9)。
//
// owner が監査者から受け取った AuditResponse を、自分の MK ラップ X25519 秘密鍵で
// HPKE 復号 (worker 内) し、全体コメント / 仕訳ごとの指摘を表示する。各 response に
// 「確認済みにする」(acknowledge) と「この監査を採用確定」(package accept) の操作を
// 提供する。差戻し / 再送は同ページの送信フォームから新ラウンドを送る運用 (PR-B)。
//
// 復号は SharedWorker 内で完結し、平文 X25519 秘密鍵はメインスレッドに出ない
// (auditor 側 audit_review_renderer と対称)。auditor が書いたコメントは要素テキスト
// コンテキストに _esc して挿入する (属性へは入れない)。
//
// 純粋ロジック (responsesForGrant / parseResponse) は DOM/crypto 非依存で export し
// node --test で単体検証する。

function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}

function getSharedWorkerUrl() {
  return globalThis.IIKANJI_SHARED_WORKER_URL || "/static/js/crypto/shared-worker.js";
}

function _csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

// ---- 純粋ロジック (テスト対象) -----------------------------------------

/**
 * この grant のパッケージに属する response のみを新しい順 (created_at 降順) に返す。
 * @param {Array<{audit_package_id:number, created_at:?string}>} responses
 * @param {Array<number>} packageIds  この grant のパッケージ id 群
 * @returns {Array}
 */
export function responsesForGrant(responses, packageIds) {
  const ids = new Set(packageIds);
  return (responses || [])
    .filter((r) => ids.has(r.audit_package_id))
    .slice()
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

/**
 * 復号した平文バイト列を AuditResponse JSON へ復元する。
 * @param {Uint8Array} plaintextBytes
 * @returns {Object}
 */
export function parseResponse(plaintextBytes) {
  return JSON.parse(new TextDecoder().decode(plaintextBytes));
}

/**
 * 旧仕訳 (owner が自分の MK で復号した現行仕訳) と auditor の構造化修正案 (§14.9)
 * を突合し、フィールド単位の差分を返す純粋関数。明細は位置 (index) で対応付ける
 * (proposal エディタが旧明細をその場で編集する前提と整合)。
 *
 * @param {?Object} oldEntry  {date, description, lines:[{account_code, debit, credit, description}]}
 * @param {Object} proposal   {date, description, lines:[{account_code, debit, credit, description?}]}
 * @returns {{date:Object, description:Object, lines:Array}}
 */
export function computeEntryDiff(oldEntry, proposal) {
  const o = oldEntry || {};
  const p = proposal || {};
  const oldLines = o.lines || [];
  const newLines = p.lines || [];
  const n = Math.max(oldLines.length, newLines.length);
  const lines = [];
  for (let i = 0; i < n; i++) {
    const ol = oldLines[i] || null;
    const nl = newLines[i] || null;
    if (ol && nl) {
      const fields = {
        account_code: String(ol.account_code ?? "") !== String(nl.account_code ?? ""),
        debit: Number(ol.debit ?? 0) !== Number(nl.debit ?? 0),
        credit: Number(ol.credit ?? 0) !== Number(nl.credit ?? 0),
        description: String(ol.description ?? "") !== String(nl.description ?? ""),
      };
      const changed = fields.account_code || fields.debit || fields.credit || fields.description;
      lines.push({ status: changed ? "changed" : "unchanged", old: ol, new: nl, fields });
    } else if (nl) {
      lines.push({ status: "added", old: null, new: nl, fields: null });
    } else {
      lines.push({ status: "removed", old: ol, new: null, fields: null });
    }
  }
  return {
    date: {
      old: o.date ?? null,
      new: p.date ?? null,
      changed: String(o.date ?? "") !== String(p.date ?? ""),
    },
    description: {
      old: o.description ?? null,
      new: p.description ?? null,
      changed: String(o.description ?? "") !== String(p.description ?? ""),
    },
    lines,
  };
}

// ---- DOM ヘルパー -------------------------------------------------------

function _esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function _status(msg, type = "info") {
  const el = document.getElementById("audit-responses-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " small";
  el.classList.remove("d-none");
}

// ---- データ取得 --------------------------------------------------------

async function _fetchOwnerPackages(grantId) {
  const r = await fetch("/api/v1/audit-packages?role=owner", {
    credentials: "same-origin",
  });
  if (!r.ok) return [];
  const d = await r.json();
  return (d.audit_packages || []).filter((p) => p.audit_grant_id === grantId);
}

async function _fetchResponses() {
  const r = await fetch("/api/v1/audit-responses", { credentials: "same-origin" });
  if (!r.ok) return [];
  const d = await r.json();
  return d.audit_responses || [];
}

// ---- レンダリング ------------------------------------------------------

function _yen(n) {
  return Number(n || 0).toLocaleString("ja-JP");
}

// proposal を持つ comment には差分の描画先プレースホルダを置き、後で
// (DOM 挿入後に) fetchEntryForDiff → computeEntryDiff で中身を埋める。
// proposal の中身 (科目コード/金額) は owner 制御外なので属性へは入れず、
// diffJobs 配列に退避して id だけで結びつける (id はこちらが採番する安全値)。
function _commentsHtml(payload, diffJobs) {
  const comments = (payload && payload.comments) || [];
  if (comments.length === 0) return "";
  const rows = comments
    .map((c) => {
      const ref = c.entry_id != null
        ? `仕訳 #${_esc(c.entry_id)}${c.ref ? "（" + _esc(c.ref) + "）" : ""}`
        : "全体";
      const note = c.note ? `: ${_esc(c.note)}` : "";
      let proposalHtml = "";
      if (c.proposal && c.entry_id != null) {
        const id = `audit-diff-${diffJobs.length}`;
        diffJobs.push({ id, entryId: c.entry_id, proposal: c.proposal });
        proposalHtml =
          `<div class="audit-proposal-diff border rounded p-2 mt-1" id="${id}">` +
          '<span class="text-muted small">修正案の差分を読み込み中…</span></div>';
      }
      return `<li class="mb-2"><strong>${ref}</strong>${note}${proposalHtml}</li>`;
    })
    .join("");
  return `<ul class="mb-2 list-unstyled">${rows}</ul>`;
}

// ---- 構造化差分テーブル (§14.9) ----------------------------------------

function _acctLabel(accountsMeta, code) {
  if (code == null || code === "") return "";
  const m = (accountsMeta || {})[code];
  return m && m.name ? `${code} ${m.name}` : String(code);
}

// 旧→新 を 1 セルに描画する。値はすべて textContent 経由 (XSS 安全)。
function _diffCell(oldText, newText, ln, fieldChanged, isNum) {
  const td = document.createElement("td");
  if (isNum) td.className = "text-end";
  if (ln.status === "added") {
    td.textContent = newText;
    td.classList.add("text-success");
  } else if (ln.status === "removed") {
    td.textContent = oldText;
    td.classList.add("text-muted", "text-decoration-line-through");
  } else if (ln.status === "changed" && fieldChanged) {
    const o = document.createElement("span");
    o.className = "text-muted text-decoration-line-through";
    o.textContent = oldText;
    const nw = document.createElement("span");
    nw.className = "text-danger";
    nw.textContent = newText;
    td.append(o, document.createTextNode(" → "), nw);
  } else {
    td.textContent = newText;
  }
  return td;
}

function _metaChangeRow(label, oldV, newV) {
  const div = document.createElement("div");
  div.className = "small mb-1";
  const strong = document.createElement("strong");
  strong.textContent = `${label}: `;
  const o = document.createElement("span");
  o.className = "text-muted text-decoration-line-through";
  o.textContent = oldV == null ? "" : String(oldV);
  const nw = document.createElement("span");
  nw.className = "text-danger";
  nw.textContent = newV == null ? "" : String(newV);
  div.append(strong, o, document.createTextNode(" → "), nw);
  return div;
}

function _diffLineRow(ln, accountsMeta) {
  const tr = document.createElement("tr");
  const f = ln.fields || {};

  const status = document.createElement("td");
  status.className = "small";
  status.textContent =
    { added: "追加", removed: "削除", changed: "変更", unchanged: "" }[ln.status] || "";
  if (ln.status === "added") status.classList.add("text-success");
  else if (ln.status === "removed") status.classList.add("text-danger");
  tr.appendChild(status);

  tr.appendChild(
    _diffCell(
      _acctLabel(accountsMeta, ln.old && ln.old.account_code),
      _acctLabel(accountsMeta, ln.new && ln.new.account_code),
      ln, f.account_code, false,
    ),
  );
  tr.appendChild(
    _diffCell(
      ln.old ? _yen(ln.old.debit) : "",
      ln.new ? _yen(ln.new.debit) : "",
      ln, f.debit, true,
    ),
  );
  tr.appendChild(
    _diffCell(
      ln.old ? _yen(ln.old.credit) : "",
      ln.new ? _yen(ln.new.credit) : "",
      ln, f.credit, true,
    ),
  );
  tr.appendChild(
    _diffCell(
      ln.old ? ln.old.description || "" : "",
      ln.new ? ln.new.description || "" : "",
      ln, f.description, false,
    ),
  );
  return tr;
}

// computeEntryDiff の結果を表 (科目/借方/貸方/摘要) として DOM 構築する。
// 全ての値は要素テキストコンテキストにのみ置く (科目名・金額・摘要)。
function _diffTableEl(diff, accountsMeta) {
  const wrap = document.createElement("div");
  if (diff.date.changed) wrap.appendChild(_metaChangeRow("日付", diff.date.old, diff.date.new));
  if (diff.description.changed) {
    wrap.appendChild(_metaChangeRow("摘要", diff.description.old, diff.description.new));
  }

  const table = document.createElement("table");
  table.className = "table table-sm mb-0 mt-1";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const h of ["", "科目", "借方", "貸方", "摘要"]) {
    const th = document.createElement("th");
    th.textContent = h;
    if (h === "借方" || h === "貸方") th.className = "text-end";
    htr.appendChild(th);
  }
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const ln of diff.lines) tbody.appendChild(_diffLineRow(ln, accountsMeta));
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// プレースホルダに現行仕訳との差分を描画する。owner 自身の MK で現行仕訳を復号
// (fetchEntryForDiff) し computeEntryDiff で突合する。取得失敗は局所表示に留める。
async function _renderDiffJob(job, client, cfg, fetchEntryForDiff) {
  const el = document.getElementById(job.id);
  if (!el) return;
  try {
    const oldEntry = await fetchEntryForDiff({
      client, userId: cfg.owner_id, entryId: job.entryId,
    });
    const diff = computeEntryDiff(oldEntry, job.proposal);
    el.innerHTML = "";
    el.appendChild(_diffTableEl(diff, cfg.accounts_meta || {}));
  } catch (e) {
    el.textContent = `修正案の差分を表示できませんでした（${e.message || e}）。`;
  }
}

function _cardHtml(r, payload, diffJobs) {
  const typeBadge = payload.response_type === "rejection"
    ? '<span class="badge bg-secondary">差戻し / 問題なし</span>'
    : '<span class="badge bg-warning text-dark">修正案</span>';
  const created = r.created_at ? _esc(r.created_at.slice(0, 10)) : "";
  const acked = r.owner_acknowledged_at
    ? '<span class="badge bg-success ms-2">確認済み</span>'
    : "";
  const summary = payload.summary
    ? `<p class="mb-2">${_esc(payload.summary)}</p>`
    : "";
  const ackBtn = r.owner_acknowledged_at
    ? ""
    : `<button type="button" class="btn btn-sm btn-outline-success"
         data-action="ack" data-response-id="${_esc(r.id)}">確認済みにする</button>`;
  return `<div class="card mb-2" data-response-card="${_esc(r.id)}">
    <div class="card-body">
      <div class="mb-2">${typeBadge}${acked}
        <span class="text-muted small ms-2">${created} 受信</span></div>
      ${summary}
      ${_commentsHtml(payload, diffJobs)}
      <div class="d-flex gap-2 mt-2" data-response-actions="${_esc(r.id)}">
        ${ackBtn}
        <button type="button" class="btn btn-sm btn-outline-primary"
          data-action="accept" data-package-id="${_esc(r.audit_package_id)}">この監査を採用確定</button>
      </div>
    </div>
  </div>`;
}

function _renderError(r, message) {
  return `<div class="card mb-2"><div class="card-body text-muted small">
    受信 #${_esc(r.id)} を復号できませんでした（${_esc(message)}）。
  </div></div>`;
}

// ---- 操作 (acknowledge / accept) ---------------------------------------

async function _post(url) {
  return fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": _csrfToken() },
  });
}

function _wireActions(container) {
  container.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    btn.disabled = true;
    const action = btn.dataset.action;
    try {
      if (action === "ack") {
        const id = btn.dataset.responseId;
        const resp = await _post(`/api/v1/audit-responses/${id}/acknowledge`);
        if (resp.ok) {
          _status("確認済みにしました。", "success");
          const actions = container.querySelector(`[data-response-actions="${id}"]`);
          if (actions) btn.remove();
        } else {
          _status(`操作に失敗しました (HTTP ${resp.status})。`, "danger");
          btn.disabled = false;
        }
      } else if (action === "accept") {
        const pid = btn.dataset.packageId;
        const resp = await _post(`/api/v1/audit-packages/${pid}/accept`);
        if (resp.ok) {
          _status("この監査を採用確定しました。", "success");
          btn.disabled = false;
        } else {
          _status(`操作に失敗しました (HTTP ${resp.status})。`, "danger");
          btn.disabled = false;
        }
      }
    } catch (err) {
      _status(`操作中にエラーが発生しました: ${err.message || err}`, "danger");
      btn.disabled = false;
    }
  });
}

// ---- メイン ------------------------------------------------------------

/**
 * owner 側の修正案レビューを初期化する。
 * cfg = { static_root, grant_id, owner_id }
 */
export async function initResponsesReview(cfg) {
  const container = document.getElementById("audit-responses-review");
  if (!container) return;

  const [packages, responses] = await Promise.all([
    _fetchOwnerPackages(cfg.grant_id),
    _fetchResponses(),
  ]);
  const mine = responsesForGrant(responses, packages.map((p) => p.id));
  if (mine.length === 0) {
    container.innerHTML =
      '<span class="text-muted small">監査者からの修正案 / 差戻しはまだありません。</span>';
    return;
  }

  // MK 解錠 + 自分のラップ秘密鍵。
  const { SharedCryptoClient } = await import(
    getStaticRoot() + "js/crypto/shared-client.js"
  );
  const client = new SharedCryptoClient(getSharedWorkerUrl());
  const st = await client.status();
  if (!st.hasKey) {
    _status("暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除すると復号できます。", "warning");
    return;
  }
  const { getKeyPair, privateKeyAAD } = await import(
    getStaticRoot() + "js/crypto/keypair.js"
  );
  const kp = await getKeyPair();
  if (!kp.encrypted_private_key) {
    _status("暗号鍵 (X25519) が未設定のため復号できません。", "warning");
    return;
  }

  const [{ responseAAD }, { b64decode }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/audit_hpke.js"),
    import(getStaticRoot() + "js/crypto/b64.js"),
  ]);

  const privAad = privateKeyAAD(cfg.owner_id);
  const htmlParts = [];
  const diffJobs = [];
  for (const r of mine) {
    try {
      const res = await client.hpkeOpen({
        encryptedPrivateKey: kp.encrypted_private_key,
        privIv: kp.private_key_iv,
        privAad,
        enc: b64decode(r.ephemeral_pubkey),
        ciphertext: b64decode(r.ciphertext),
        aad: responseAAD(r.audit_package_id),
      });
      htmlParts.push(_cardHtml(r, parseResponse(res.plaintext), diffJobs));
    } catch (err) {
      htmlParts.push(_renderError(r, err.message || String(err)));
    }
  }
  container.innerHTML = htmlParts.join("");
  _wireActions(container);

  // 構造化修正案 (§14.9) の差分を、各プレースホルダに後追いで描画する。
  // 現行仕訳の取得・復号は owner 自身の MK 経路 (fetchEntryForDiff)。プレースホルダ
  // は既に DOM 上にあるので、複数 proposal の取得は並列化して往復遅延を抑える。
  if (diffJobs.length) {
    const { fetchEntryForDiff } = await import(
      getStaticRoot() + "js/crypto/journals_client.js"
    );
    await Promise.all(
      diffJobs.map((job) => _renderDiffJob(job, client, cfg, fetchEntryForDiff)),
    );
  }
}
