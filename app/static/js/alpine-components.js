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

  /**
   * 月次確定チェック + 計上期間による日付自動設定
   *
   * 使い方:
   *   <div x-data="fiscalPeriodChecker({
   *     closedPeriods: { ... },
   *     restrictedBefore: 2024,
   *     dateValue: '2026-01-15',
   *     fiscalPeriod: ''        // optional: 計上期間セレクト連動
   *   })">
   */
  Alpine.data('fiscalPeriodChecker', function(config) {
    return {
      closedPeriods: config.closedPeriods || {},
      restrictedBefore: config.restrictedBefore,
      dateValue: config.dateValue || '',
      fiscalPeriod: config.fiscalPeriod != null ? String(config.fiscalPeriod) : '',
      dateReadOnly: false,
      warningMessage: '',
      isBlocked: false,

      init: function() {
        this.onFiscalPeriodChange();
      },

      checkDate: function() {
        if (!this.dateValue) {
          this.warningMessage = '';
          this.isBlocked = false;
          return;
        }
        var year = parseInt(this.dateValue.substring(0, 4));
        var month = parseInt(this.dateValue.substring(5, 7));
        var period = this.fiscalPeriod !== '' ? parseInt(this.fiscalPeriod) : month;
        var msg = null;

        if (this.restrictedBefore && year < this.restrictedBefore && this.closedPeriods[year] === undefined) {
          msg = year + '年度は開設されていません。';
        }
        var closed = this.closedPeriods[year];
        if (!msg && closed !== undefined && period <= closed) {
          var label = period === 0 ? '期首' : period <= 12 ? period + '月' : '決算整理' + (period - 12);
          msg = year + '年 ' + label + ' は月次確定済みです。この日付では保存できません。';
        }

        this.warningMessage = msg || '';
        this.isBlocked = !!msg;
      },

      onFiscalPeriodChange: function() {
        if (!this.dateValue) { this.checkDate(); return; }
        var year = this.dateValue.substring(0, 4);
        var fp = this.fiscalPeriod;
        if (fp === '0') {
          this.dateValue = year + '-01-01';
          this.dateReadOnly = true;
        } else if (fp === '13' || fp === '14' || fp === '15') {
          this.dateValue = year + '-12-31';
          this.dateReadOnly = true;
        } else {
          this.dateReadOnly = false;
        }
        this.checkDate();
      }
    };
  });

  /**
   * 仕訳帳一覧: 一括選択 + ドラッグ選択連携
   *
   * 使い方:
   *   <form x-data="bulkSelect" ...>
   */
  Alpine.data('bulkSelect', function() {
    return {
      selectedCount: 0,
      allSelected: false,

      init: function() {
        this.updateCount();
        var self = this;
        document.addEventListener('htmx:afterSwap', function() { self.updateCount(); });
      },

      updateCount: function() {
        var checked = this.$el.querySelectorAll('.entry-cb:checked').length;
        var total = this.$el.querySelectorAll('.entry-cb').length;
        this.selectedCount = checked;
        this.allSelected = checked === total && total > 0;
      },

      toggleAll: function() {
        var cbs = this.$el.querySelectorAll('.entry-cb');
        var val = this.allSelected;
        cbs.forEach(function(cb) { cb.checked = val; });
        this.updateCount();
      },

      confirmBulkDelete: function() {
        var withVoucher = this.$el.querySelectorAll('.entry-cb:checked[data-has-voucher="true"]').length;
        var msg = withVoucher > 0
          ? withVoucher + '件の仕訳に証憑が紐づいています。削除すると証憑が未紐付けになります。削除しますか？'
          : '選択した仕訳を削除しますか？この操作は取り消せません。';
        return confirm(msg);
      }
    };
  });

});
