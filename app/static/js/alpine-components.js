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

});
