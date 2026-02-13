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
