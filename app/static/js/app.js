// 金額入力フィールドでEnterキーを押した時のフォーム送信を防止（仕訳フォーム）
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('input[type="number"]').forEach(function(input) {
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
      }
    });
  });
});

/**
 * 証憑画像をモーダルでプレビュー表示（PWA対応）
 */
function openImagePreview(url) {
  var modal = document.getElementById('imagePreviewModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'imagePreviewModal';
    modal.className = 'modal fade';
    modal.tabIndex = -1;
    modal.innerHTML =
      '<div class="modal-dialog modal-lg modal-dialog-centered">' +
        '<div class="modal-content bg-dark border-0">' +
          '<div class="modal-header border-0 py-2">' +
            '<button type="button" id="imagePreviewNewTab" class="btn btn-sm btn-outline-light ms-auto me-2" title="別タブで開く">' +
              '<i class="bi bi-box-arrow-up-right"></i></button>' +
            '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>' +
          '</div>' +
          '<div class="modal-body text-center p-2">' +
            '<img id="imagePreviewImg" class="img-fluid" style="max-height:85vh;" alt="証憑">' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
  }
  document.getElementById('imagePreviewImg').src = url;
  document.getElementById('imagePreviewNewTab').onclick = function() {
    window.open(url, '_blank', 'noopener');
  };
  new bootstrap.Modal(modal).show();
}

/**
 * 証憑プレビューを描画する共通関数
 * @param {HTMLElement} container - 描画先の要素
 * @param {Array} vouchers - [{id, uploaded_at}, ...] 形式の証憑データ
 * @param {string} baseUrl - 証憑画像エンドポイントのベース（例: "/ai-journal/voucher/"）
 */
function renderVoucherPreview(container, vouchers, baseUrl) {
  if (!container || !vouchers || vouchers.length === 0) {
    if (container) container.classList.add('d-none');
    return;
  }
  baseUrl = baseUrl || '/ai-journal/voucher/';
  var html = '<div class="card shadow-sm mt-3 mb-0">' +
    '<div class="card-header"><i class="bi bi-file-image"></i> 証憑画像</div>' +
    '<div class="card-body text-center">';
  vouchers.forEach(function(v) {
    var url = baseUrl + v.id + '/image';
    var thumbUrl = url + '?size=thumb';
    // E4 PR-C2: 暗号化証憑はサーバが暗号文 (octet-stream) を返すため <img> で
    // 直接表示できない。読み込み失敗 (onerror) 時はロック表示に差し替え、証憑
    // 一覧 (クライアント復号表示) への導線を出す。完全な復号インライン表示は
    // 後続 PR (C3) で対応する。
    html += '<a href="#" onclick="openImagePreview(\'' + url + '\');return false">' +
      '<img src="' + thumbUrl + '" class="img-fluid rounded" style="max-height:300px;cursor:pointer;" alt="証憑" loading="lazy" ' +
      'onerror="voucherThumbFallback(this)">' +
      '</a> ';
  });
  html += '</div></div>';
  container.innerHTML = html;
  container.classList.remove('d-none');
}

/**
 * 証憑サムネイルの読み込み失敗時フォールバック (E4 PR-C2)。
 * 暗号化証憑は <img> で表示できないため、ロックアイコン + 証憑一覧への導線に
 * 差し替える。
 */
function voucherThumbFallback(img) {
  var anchor = img.closest('a');
  var ph = document.createElement('span');
  ph.className = 'd-inline-flex flex-column align-items-center text-muted border rounded p-3';
  ph.innerHTML =
    '<i class="bi bi-lock-fill" style="font-size:1.8rem;"></i>' +
    '<span class="small mt-1">暗号化証憑</span>' +
    '<a href="/vouchers" class="small">証憑一覧で表示</a>';
  if (anchor) {
    anchor.replaceWith(ph);
  } else {
    img.replaceWith(ph);
  }
}
