// E5 (#111): AI 下書き一覧 (drafts.html) の暗号化サムネをクライアント復号して
// 表示する。
//
// 暗号化下書きの画像配信 (/ai-journal/drafts/<id>/image) は octet-stream を返す
// ため <img src> 直貼りでは表示できない。本モジュールは Jinja が出力した
// `<img.ai-draft-thumb data-draft-id data-aad-id>` を走査し、MK で復号した
// サムネを blob URL に変換して src へ差し替える。
//
// MK ロック / 代理閲覧 (オーナーの MK 非保持) では復号できないため、ロック表示
// (鍵アイコン) に差し替える。レガシー平文下書き (encrypted_meta_blob 無し) は
// サーバが画像を直接配信するので本モジュールの対象外 (Jinja 側で通常 img)。
//
// crypto/ を DOM 非依存に保つため、本 DOM 操作モジュールは ai_journal/ に置く
// (Node の crypto カバレッジゲート対象外、Playwright E2E で検証)。

function _staticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _sharedWorkerUrl() {
  return globalThis.IIKANJI_SHARED_WORKER_URL
    || "/static/js/crypto/shared-worker.js";
}


function _lockPlaceholder(img, label) {
  const ph = document.createElement("div");
  ph.className =
    "text-muted border rounded d-flex flex-column align-items-center "
    + "justify-content-center mx-auto";
  ph.style.height = "150px";
  ph.style.maxWidth = "150px";
  ph.innerHTML = '<i class="bi bi-lock-fill" style="font-size:1.5rem;"></i>';
  const span = document.createElement("div");
  span.className = "small mt-1";
  span.textContent = label;
  ph.appendChild(span);
  if (img.parentNode) img.parentNode.replaceChild(ph, img);
}


async function _run() {
  const imgs = Array.from(document.querySelectorAll("img.ai-draft-thumb"));
  if (imgs.length === 0) return;

  const userId = globalThis.IIKANJI_AI_DRAFTS_USERID;
  if (typeof userId !== "number") return;

  const root = _staticRoot();
  let client = null;
  try {
    const [{ SharedCryptoClient }, draftDl] = await Promise.all([
      import(root + "js/crypto/shared-client.js"),
      import(root + "js/crypto/ai_draft_download.js"),
    ]);

    client = new SharedCryptoClient(_sharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      // MK ロック / 代理閲覧: 復号不能。全サムネをロック表示に。
      for (const img of imgs) _lockPlaceholder(img, "暗号鍵が必要です");
      return;
    }

    await Promise.all(imgs.map(async (img) => {
      const draftId = Number(img.getAttribute("data-draft-id"));
      const aadRaw = img.getAttribute("data-aad-id");
      if (!Number.isInteger(draftId) || aadRaw == null || aadRaw === "") {
        _lockPlaceholder(img, "復号に失敗しました");
        return;
      }
      try {
        const bytes = await draftDl.fetchAndDecryptDraftImage({
          client, userId, draftId, aadId: BigInt(aadRaw), thumb: true,
        });
        const url = URL.createObjectURL(
          new Blob([bytes], { type: draftDl.sniffImageMime(bytes) }),
        );
        // 表示完了後に revoke してメモリリークを防ぐ。描画失敗時 (改ざん /
        // MIME 不一致 / 破損) も onerror で revoke + ロック表示に差し替える
        // (PR-3 レビュー指摘: onload だけだと失敗時に blob URL がリーク)。
        img.onload = () => {
          try { URL.revokeObjectURL(url); } catch (_e) { /* ignore */ }
        };
        img.onerror = () => {
          try { URL.revokeObjectURL(url); } catch (_e) { /* ignore */ }
          _lockPlaceholder(img, "復号に失敗しました");
        };
        img.src = url;
        img.alt = "証憑画像";
      } catch (_e) {
        _lockPlaceholder(img, "復号に失敗しました");
      }
    }));
  } catch (_e) {
    for (const img of imgs) _lockPlaceholder(img, "復号に失敗しました");
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
