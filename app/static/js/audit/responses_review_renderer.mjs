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

function _commentsHtml(payload) {
  const comments = (payload && payload.comments) || [];
  if (comments.length === 0) return "";
  const rows = comments
    .map((c) => {
      const ref = c.entry_id != null
        ? `仕訳 #${_esc(c.entry_id)}${c.ref ? "（" + _esc(c.ref) + "）" : ""}`
        : "全体";
      return `<li><strong>${ref}:</strong> ${_esc(c.note)}</li>`;
    })
    .join("");
  return `<ul class="mb-2">${rows}</ul>`;
}

function _cardHtml(r, payload) {
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
      ${_commentsHtml(payload)}
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
      htmlParts.push(_cardHtml(r, parseResponse(res.plaintext)));
    } catch (err) {
      htmlParts.push(_renderError(r, err.message || String(err)));
    }
  }
  container.innerHTML = htmlParts.join("");
  _wireActions(container);
}
