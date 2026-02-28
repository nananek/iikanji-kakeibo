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
    html += '<a href="' + url + '" target="_blank">' +
      '<img src="' + url + '" class="img-fluid rounded" style="max-height:300px;" alt="証憑" loading="lazy">' +
      '</a> ';
  });
  html += '</div></div>';
  container.innerHTML = html;
  container.classList.remove('d-none');
}
