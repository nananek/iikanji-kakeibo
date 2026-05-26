// Phase E3-D-0b: クライアントサイド accounting helpers。
//
// サーバ側 app/services/accounting.create_cashbook_entry /
// create_transfer_entry に相当するロジックを JS 純粋関数として実装する。
// CSV/OFX/Web 取込のクライアント完結 E2EE 化 (E3-D-1b/2b/3b) で、
// クライアントが仕訳化までを担い、結果を batch API に POST する用途。
//
// 戻り値: POST /api/v1/journals/batch の entries[] にそのまま push できる形:
//   {date, description, source, fiscal_period, lines: [
//     {account_code, debit, credit},
//     {account_code, debit, credit},
//   ]}
//
// batch_id は entry レベルでは持たず、batch API のリクエスト top-level で
// 1 つだけ指定する。fiscal_period が null の場合はサーバ側 service が
// date.month から自動判定する (CLAUDE.md 参照)。
//
// debit/credit は必ず int (Math.abs で正規化)。batch API は float/bool を
// 拒否する仕様なので Math.round で integer 化はしない (呼出側が int で渡す
// 責務、誤入力を黙って丸めない fail-loud 設計)。


function _assertIntAmount(amount, label) {
  if (!Number.isInteger(amount)) {
    throw new TypeError(`${label} must be an integer (got ${typeof amount})`);
  }
  if (amount === 0) {
    throw new TypeError(`${label} must be a non-zero integer`);
  }
}


function _assertNonEmptyString(value, label) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
}


function _assertFiscalPeriod(fiscalPeriod) {
  if (fiscalPeriod === null || fiscalPeriod === undefined) return;
  // 16 (損益振替) は自動生成専用 (CLAUDE.md)、batch API も拒否する。
  // クライアント側でも fail-loud で早期に弾く。
  if (!Number.isInteger(fiscalPeriod) || fiscalPeriod < 0 || fiscalPeriod > 15) {
    throw new TypeError(
      `fiscalPeriod must be an integer 0-15 or null (got ${fiscalPeriod})`,
    );
  }
}


/**
 * 出納帳の入力から仕訳 entry を生成する純粋関数。
 *
 * @param {Object} opts
 * @param {string} opts.date                  ISO 形式 (YYYY-MM-DD)
 * @param {string} [opts.description=""]
 * @param {"income"|"expense"} opts.transactionType
 * @param {string} opts.paymentAccountCode    支払元/入金先 (資産・負債)
 * @param {string} opts.categoryAccountCode   費目 (収益・費用)
 * @param {number} opts.amount                非ゼロ整数 (負なら借方・貸方が反転)
 * @param {string} [opts.source="cashbook"]
 * @param {number|null} [opts.fiscalPeriod=null]   0-15 (16=損益振替は不可)
 * @returns {Object} batch API entries[] 用の entry オブジェクト
 */
export function buildCashbookEntry({
  date,
  description = "",
  transactionType,
  paymentAccountCode,
  categoryAccountCode,
  amount,
  source = "cashbook",
  fiscalPeriod = null,
}) {
  _assertNonEmptyString(date, "date");
  if (transactionType !== "income" && transactionType !== "expense") {
    throw new TypeError(
      `transactionType must be 'income' or 'expense' (got ${transactionType})`,
    );
  }
  _assertNonEmptyString(paymentAccountCode, "paymentAccountCode");
  _assertNonEmptyString(categoryAccountCode, "categoryAccountCode");
  _assertIntAmount(amount, "amount");
  _assertFiscalPeriod(fiscalPeriod);

  const absAmount = Math.abs(amount);
  let debitCode, creditCode;
  if (transactionType === "expense") {
    debitCode = categoryAccountCode;
    creditCode = paymentAccountCode;
  } else {
    debitCode = paymentAccountCode;
    creditCode = categoryAccountCode;
  }
  if (amount < 0) {
    const t = debitCode;
    debitCode = creditCode;
    creditCode = t;
  }

  return {
    date,
    description,
    source,
    fiscal_period: fiscalPeriod,
    lines: [
      { account_code: debitCode, debit: absAmount, credit: 0 },
      { account_code: creditCode, debit: 0, credit: absAmount },
    ],
  };
}


/**
 * 口座間振替の仕訳 entry を生成する純粋関数。
 *
 * @param {Object} opts
 * @param {string} opts.date
 * @param {string} [opts.description=""]
 * @param {string} opts.fromAccountCode      出金元 (貸方)
 * @param {string} opts.toAccountCode        入金先 (借方)
 * @param {number} opts.amount               非ゼロ整数 (負なら借方・貸方が反転)
 * @param {string} [opts.source="cashbook"]
 * @param {number|null} [opts.fiscalPeriod=null]
 * @returns {Object}
 */
export function buildTransferEntry({
  date,
  description = "",
  fromAccountCode,
  toAccountCode,
  amount,
  source = "cashbook",
  fiscalPeriod = null,
}) {
  _assertNonEmptyString(date, "date");
  _assertNonEmptyString(fromAccountCode, "fromAccountCode");
  _assertNonEmptyString(toAccountCode, "toAccountCode");
  _assertIntAmount(amount, "amount");
  _assertFiscalPeriod(fiscalPeriod);
  if (fromAccountCode === toAccountCode) {
    throw new TypeError(
      "fromAccountCode and toAccountCode must differ",
    );
  }

  const absAmount = Math.abs(amount);
  let debitCode = toAccountCode;
  let creditCode = fromAccountCode;
  if (amount < 0) {
    debitCode = fromAccountCode;
    creditCode = toAccountCode;
  }

  return {
    date,
    description,
    source,
    fiscal_period: fiscalPeriod,
    lines: [
      { account_code: debitCode, debit: absAmount, credit: 0 },
      { account_code: creditCode, debit: 0, credit: absAmount },
    ],
  };
}
