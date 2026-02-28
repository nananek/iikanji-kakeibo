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

  /**
   * 勘定科目管理: 科目追加・編集モーダル
   *
   * 使い方:
   *   <div x-data="accountEditor({
   *     expenseTypeId: 5, revenueTypeId: 4,
   *     defaultTypeId: 1, accountsByType: { ... }
   *   })">
   */
  Alpine.data('accountEditor', function(config) {
    return {
      expenseTypeId: config.expenseTypeId,
      revenueTypeId: config.revenueTypeId,
      accountsByType: config.accountsByType,
      editingId: null,
      editingTypeId: null,
      wasActive: true,
      code: '',
      name: '',
      accountTypeId: String(config.defaultTypeId),
      description: '',
      taxCategory: '',
      costType: '',
      isActive: true,
      codeReadOnly: false,
      typeDisabled: false,
      activeDisabled: false,
      errorMessage: '',
      saving: false,
      modalTitle: '科目を追加',
      modal: null,
      showDeactivate: false,
      deactivateLoading: false,
      balanceLabel: '',
      hasBalance: false,
      transferToId: '',
      transferCandidates: [],

      get showCostType() {
        var t = parseInt(this.accountTypeId);
        return t === this.expenseTypeId || t === this.revenueTypeId;
      },
      get costLabel() {
        return parseInt(this.accountTypeId) === this.revenueTypeId ? '収入区分' : '費用区分';
      },

      getModal: function() {
        if (!this.modal) this.modal = new bootstrap.Modal(document.getElementById('accountModal'));
        return this.modal;
      },

      resetForm: function() {
        this.editingId = null;
        this.editingTypeId = null;
        this.wasActive = true;
        this.code = '';
        this.name = '';
        this.accountTypeId = String(config.defaultTypeId);
        this.description = '';
        this.taxCategory = '';
        this.costType = '';
        this.isActive = true;
        this.codeReadOnly = false;
        this.typeDisabled = false;
        this.activeDisabled = false;
        this.errorMessage = '';
        this.saving = false;
        this.showDeactivate = false;
        this.transferToId = '';
      },

      open: function(accountId, copy) {
        this.resetForm();
        if (accountId) {
          var self = this;
          var url = '/accounts/api/' + accountId;
          if (copy) url += '?copy=1';
          fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
              self.editingId = data.id;
              self.editingTypeId = data.account_type_id;
              self.wasActive = data.is_active;
              self.modalTitle = copy ? 'コピーして追加' : '科目を編集';
              self.code = data.code;
              self.codeReadOnly = !copy && data.is_system;
              self.name = data.name;
              self.accountTypeId = String(data.account_type_id);
              self.typeDisabled = !copy && data.is_system;
              self.description = data.description;
              self.taxCategory = data.tax_category;
              self.costType = data.cost_type;
              self.isActive = data.is_active;
              self.activeDisabled = !copy && !!data.system_role;
              self.getModal().show();
            });
        } else {
          this.modalTitle = '科目を追加';
          this.getModal().show();
        }
      },

      onActiveChange: function() {
        if (!this.isActive && this.editingId && this.wasActive) {
          this.showDeactivate = true;
          this.deactivateLoading = true;
          this.hasBalance = false;
          var self = this;
          fetch('/accounts/api/' + this.editingId + '/balance')
            .then(function(r) { return r.json(); })
            .then(function(data) {
              self.deactivateLoading = false;
              if (data.balance !== 0) {
                self.hasBalance = true;
                self.balanceLabel = '\u00a5' + Math.abs(data.balance).toLocaleString() +
                  (data.balance < 0 ? '（貸方残）' : '（借方残）');
                var candidates = self.accountsByType[self.editingTypeId] || [];
                self.transferCandidates = candidates.filter(function(a) {
                  return a.id !== self.editingId;
                });
              }
            });
        } else {
          this.showDeactivate = false;
        }
      },

      save: function() {
        this.errorMessage = '';
        var payload = {
          code: this.code,
          name: this.name,
          account_type_id: parseInt(this.accountTypeId),
          description: this.description,
          tax_category: this.taxCategory,
          cost_type: this.costType,
          is_active: this.isActive,
        };
        if (!this.isActive && this.editingId && this.wasActive && this.transferToId) {
          payload.transfer_to_account_id = parseInt(this.transferToId);
        }
        var url = this.editingId ? ('/accounts/api/' + this.editingId) : '/accounts/api/new';
        this.saving = true;
        var self = this;
        fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content,
          },
          body: JSON.stringify(payload),
        })
        .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
        .then(function(res) {
          self.saving = false;
          if (!res.ok) {
            self.errorMessage = res.data.error || 'エラーが発生しました。';
            return;
          }
          location.reload();
        })
        .catch(function() {
          self.saving = false;
          self.errorMessage = '通信エラーが発生しました。';
        });
      }
    };
  });

  /**
   * 科目選択モーダル
   *
   * 使い方:
   *   <div id="accountSelectorModal" x-data="accountSelector(_acctSelectorData)"
   *        @open-selector="open($event.detail)">
   */
  Alpine.data('accountSelector', function(allGroups) {
    var bsCodes = ['asset', 'liability', 'equity'];
    var plCodes = ['expense', 'revenue'];
    var filterMap = {
      all: ['asset', 'liability', 'equity', 'expense', 'revenue'],
      payment: ['asset', 'liability'],
      category: ['expense', 'revenue'],
      category_transfer: ['asset', 'liability', 'expense', 'revenue'],
    };

    return {
      allGroups: allGroups,
      searchQuery: '',
      activeTab: 'bs',
      currentId: null,
      filterType: 'all',
      excludeId: null,
      modal: null,

      get filteredGroups() {
        var allowed = filterMap[this.filterType] || filterMap.all;
        var exId = this.excludeId;
        return this.allGroups
          .filter(function(g) { return allowed.indexOf(g.type_code) !== -1; })
          .map(function(g) {
            if (!exId) return g;
            return {
              type_code: g.type_code, type_name: g.type_name,
              normal_balance: g.normal_balance,
              accounts: g.accounts.filter(function(a) { return a.id !== exId; }),
            };
          })
          .filter(function(g) { return g.accounts.length > 0; });
      },

      get bsGroups() {
        return this.filteredGroups.filter(function(g) { return bsCodes.indexOf(g.type_code) !== -1; });
      },
      get plGroups() {
        return this.filteredGroups.filter(function(g) { return plCodes.indexOf(g.type_code) !== -1; });
      },
      get hasBs() { return this.bsGroups.length > 0; },
      get hasPl() { return this.plGroups.length > 0; },

      groupsForSide: function(groups, side) {
        return groups.filter(function(g) { return g.normal_balance === side; });
      },

      matchesSearch: function(account) {
        if (!this.searchQuery) return true;
        var q = this.searchQuery.toLowerCase();
        return account.name.toLowerCase().indexOf(q) !== -1 ||
               account.code.toLowerCase().indexOf(q) !== -1;
      },

      open: function(options) {
        options = options || {};
        this.filterType = options.filter || 'all';
        this.excludeId = options.excludeId || null;
        this.currentId = options.currentId ? parseInt(options.currentId) : null;
        this.searchQuery = '';

        var autoTab = null;
        if (this.currentId) {
          for (var i = 0; i < this.filteredGroups.length; i++) {
            var g = this.filteredGroups[i];
            for (var j = 0; j < g.accounts.length; j++) {
              if (g.accounts[j].id === this.currentId) {
                autoTab = bsCodes.indexOf(g.type_code) !== -1 ? 'bs' : 'pl';
                break;
              }
            }
            if (autoTab) break;
          }
        }
        this.activeTab = autoTab || ((options.activeTab === 'pl' && this.hasPl) ? 'pl' : 'bs');

        if (!this.modal) this.modal = new bootstrap.Modal(this.$el);
        this.modal.show();

        if (this.currentId) {
          var el = this.$el;
          el.addEventListener('shown.bs.modal', function _scroll() {
            el.removeEventListener('shown.bs.modal', _scroll);
            var cur = el.querySelector('.acct-current');
            if (cur) cur.scrollIntoView({ block: 'center', behavior: 'instant' });
          });
        }
      },

      select: function(id, name, typeCode) {
        if (window._acctSelectorCallback) {
          window._acctSelectorCallback(id, name, typeCode);
        }
        this.modal.hide();
      }
    };
  });

});
