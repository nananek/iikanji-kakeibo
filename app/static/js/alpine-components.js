/**
 * Alpine.js 共通コンポーネント定義
 * alpine:init イベントで Alpine 起動前に登録される
 */
document.addEventListener('alpine:init', function() {

  // CSRF トークンストア（fetch呼び出し用）
  Alpine.store('csrf', {
    get token() {
      return document.querySelector('meta[name="csrf-token"]').content;
    }
  });

});
