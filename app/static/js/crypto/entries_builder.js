// Phase E3-D-0b / E3-F PR-A: クライアントサイド accounting helpers。
//
// サーバ側 app/services/accounting.create_cashbook_entry /
// create_transfer_entry に相当するロジックを JS 純粋関数として実装する。
// CSV/OFX/Web 取込 (E3-D-1b/2b/3b) と E3-F の dual-write 撤去で、
// クライアントが仕訳化までを担い、結果を batch API に POST する用途。
//
// E3-F PR-A: client + userId を受け取り、entry と各 line に
// encrypted_blob / blob_iv を MK で暗号化して付与する。AAD は Option B
// (buildAAD("je"|"jel", userId), entry_id 等を含めない)。
//
// 戻り値: POST /api/v1/journals/batch の entries[] にそのまま push できる形
// (E3-F PR-D-6-6: wire 平文除去後):
//   {
//     fiscal_year, fiscal_month,   // 平文メタ (サーバの年度/期間フィルタ用)
//     encrypted_blob, blob_iv,     // entry 本体 (date/description/source/
//                                  //   fiscal_period の暗号化版)
//     lines: [
//       {account_code, debit, credit, encrypted_blob, blob_iv},
//       {account_code, debit, credit, encrypted_blob, blob_iv},
//     ]
//   }
//
// 平文 date / description / source / fiscal_period は wire に乗せない (entry
// 本体は encrypted_blob に格納済、列は 055 で DROP 済)。サーバが平文で必要と
// するメタは fiscal_year / fiscal_month のみ = ここで date / fiscal_period から
// 算出して必ず付与する。
//
// batch_id は entry レベルでは持たず、batch API のリクエスト top-level で
// 1 つだけ指定する。
//
// debit/credit は必ず int (Math.abs で正規化)。batch API は float/bool を
// 拒否する仕様なので Math.round で integer 化はしない (呼出側が int で渡す
// 責務、誤入力を黙って丸めない fail-loud 設計)。

import { buildAAD, encryptRecord } from "./record.js";
import { b64encode } from "./b64.js";


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


function _validateUserId(userId) {
  if (typeof userId !== "number" && typeof userId !== "bigint") {
    throw new TypeError("userId must be a number or bigint");
  }
  if (typeof userId === "number" && !Number.isSafeInteger(userId)) {
    throw new TypeError(
      "userId Number must be a safe integer (use BigInt for > 2^53)",
    );
  }
}


function _fiscalYearFromDate(date) {
  // ISO YYYY-MM-DD から年部分を抽出。fiscal_year は SmallInteger 平文カラム
  // (§12.3) で、年度フィルタ用に保持する。
  const y = parseInt(date.substring(0, 4), 10);
  if (!Number.isInteger(y) || y < 1900 || y > 2200) {
    throw new TypeError(`cannot derive fiscal_year from date: ${date}`);
  }
  return y;
}


function _fiscalMonthFromEntry(entry) {
  // E3-F PR-D-6-6: 平文 fiscal_month メタを算出する。fiscalPeriod が明示
  // 指定 (0=期首 / 13-15=決算整理) ならそれを、それ以外 (通常仕訳) は date の
  // 月 (1-12) を使う。サーバはこの値を JournalEntry.fiscal_month へ直接保存し、
  // 期間ロック判定にも使う (旧サーバの fiscal_period ?? date.month と等価)。
  const fp = entry.fiscal_period;
  if (fp !== null && fp !== undefined) {
    return fp;
  }
  const m = parseInt(entry.date.substring(5, 7), 10);
  if (!Number.isInteger(m) || m < 1 || m > 12) {
    throw new TypeError(`cannot derive fiscal_month from date: ${entry.date}`);
  }
  return m;
}


/**
 * entry 本体 + 各 line を MK で暗号化し、encrypted_blob / blob_iv を付与する。
 *
 * @param {Object} client    SharedCryptoClient
 * @param {number|bigint} userId
 * @param {Object} entry     buildCashbookEntry / buildTransferEntry の戻り値
 *                           (encrypted_blob / blob_iv 未付与状態)
 * @returns {Promise<Object>}  encrypted_blob / blob_iv / fiscal_year 付き entry
 */
async function _encryptEntry(client, userId, entry) {
  const entryBody = {
    v: 1,
    date: entry.date,
    description: entry.description,
    source: entry.source,
    fiscal_period: entry.fiscal_period,
    // batch_id は entry レベルでは持たず、batch top-level に集約される。
  };
  const entryAAD = buildAAD("je", userId);
  const entryEnc = await encryptRecord(client, entryBody, entryAAD);

  const encryptedLines = [];
  const lineAAD = buildAAD("jel", userId);
  for (const line of entry.lines) {
    const lineBody = {
      v: 1,
      account_code: line.account_code,
      debit_amount: line.debit,
      credit_amount: line.credit,
      // 行摘要は line 本体 (encrypted_blob) にのみ格納する。平文 description は
      // wire に乗せない (列は 055 で DROP 済)。
      description: line.description ?? "",
    };
    const lineEnc = await encryptRecord(client, lineBody, lineAAD);
    // E3-F PR-D-6-6: wire に乗せる line の平文は account_code / debit / credit
    // (集計用の保持メタ) のみ。description は encrypted_blob へ。
    encryptedLines.push({
      account_code: line.account_code,
      debit: line.debit,
      credit: line.credit,
      encrypted_blob: b64encode(lineEnc.blob),
      blob_iv: b64encode(lineEnc.iv),
    });
  }

  // E3-F PR-D-6-6: top-level の平文 date / description / source / fiscal_period
  // は wire に乗せない。サーバが平文で要するメタは fiscal_year / fiscal_month
  // のみ。
  return {
    fiscal_year: _fiscalYearFromDate(entry.date),
    fiscal_month: _fiscalMonthFromEntry(entry),
    encrypted_blob: b64encode(entryEnc.blob),
    blob_iv: b64encode(entryEnc.iv),
    lines: encryptedLines,
  };
}


/**
 * 出納帳の入力から仕訳 entry を生成する純粋関数。
 *
 * client + userId が指定された場合は entry + 各 line を MK で暗号化して
 * encrypted_blob / blob_iv / fiscal_year を付与する (E3-F PR-A 以降の
 * 推奨経路)。client なしでも従来通り平文 entry を返す (テスト用途等)。
 *
 * @param {Object} opts
 * @param {Object} [opts.client]              SharedCryptoClient (暗号化する場合)
 * @param {number|bigint} [opts.userId]       (client 指定時に必須)
 * @param {string} opts.date                  ISO 形式 (YYYY-MM-DD)
 * @param {string} [opts.description=""]
 * @param {"income"|"expense"} opts.transactionType
 * @param {string} opts.paymentAccountCode    支払元/入金先 (資産・負債)
 * @param {string} opts.categoryAccountCode   費目 (収益・費用)
 * @param {number} opts.amount                非ゼロ整数 (負なら借方・貸方が反転)
 * @param {string} [opts.source="cashbook"]
 * @param {number|null} [opts.fiscalPeriod=null]   0-15 (16=損益振替は不可)
 * @returns {Promise<Object>|Object} batch API entries[] 用の entry オブジェクト
 *   - client 指定時: Promise<encrypted entry>
 *   - client なし:   平文 entry (同期返却)
 */
export function buildCashbookEntry({
  client,
  userId,
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

  const entry = {
    date,
    description,
    source,
    fiscal_period: fiscalPeriod,
    lines: [
      { account_code: debitCode, debit: absAmount, credit: 0 },
      { account_code: creditCode, debit: 0, credit: absAmount },
    ],
  };

  if (client !== undefined && client !== null) {
    _validateUserId(userId);
    return _encryptEntry(client, userId, entry);
  }
  return entry;
}


/**
 * 口座間振替の仕訳 entry を生成する純粋関数。
 *
 * @param {Object} opts
 * @param {Object} [opts.client]
 * @param {number|bigint} [opts.userId]
 * @param {string} opts.date
 * @param {string} [opts.description=""]
 * @param {string} opts.fromAccountCode      出金元 (貸方)
 * @param {string} opts.toAccountCode        入金先 (借方)
 * @param {number} opts.amount               非ゼロ整数 (負なら借方・貸方が反転)
 * @param {string} [opts.source="cashbook"]
 * @param {number|null} [opts.fiscalPeriod=null]
 * @returns {Promise<Object>|Object}
 */
/**
 * 仕訳帳 (journal) の N 行可変 entry を生成する純粋関数。
 *
 * cashbook (固定 2 行) と違い、lines は呼出側で組み立て済みのものを受け取る。
 * 貸借一致 (sum(debit) == sum(credit) > 0) を assert し、各 line は account_code
 * 非空 + debit/credit が非負整数 + (debit > 0 XOR credit > 0) を満たすこと。
 *
 * E3-F PR-B2: journal/form.html の form POST を撤去し、JS submit + 暗号化経路に
 * 移行する際の共通 entry builder。
 *
 * @param {Object} opts
 * @param {Object} [opts.client]              SharedCryptoClient
 * @param {number|bigint} [opts.userId]
 * @param {string} opts.date                  ISO YYYY-MM-DD
 * @param {string} [opts.description=""]
 * @param {Array<{account_code: string, debit: number, credit: number, description?: string}>} opts.lines
 * @param {string} [opts.source="journal"]
 * @param {number|null} [opts.fiscalPeriod=null]
 * @returns {Promise<Object>|Object}
 */
export function buildJournalEntry({
  client,
  userId,
  date,
  description = "",
  lines,
  source = "journal",
  fiscalPeriod = null,
}) {
  _assertNonEmptyString(date, "date");
  _assertFiscalPeriod(fiscalPeriod);
  if (!Array.isArray(lines) || lines.length < 2) {
    throw new TypeError("lines must be an array of length >= 2");
  }

  let totalDebit = 0;
  let totalCredit = 0;
  const normalizedLines = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line === null || typeof line !== "object") {
      throw new TypeError(`lines[${i}] must be an object`);
    }
    _assertNonEmptyString(line.account_code, `lines[${i}].account_code`);
    const debit = line.debit ?? 0;
    const credit = line.credit ?? 0;
    if (!Number.isInteger(debit) || debit < 0) {
      throw new TypeError(`lines[${i}].debit must be a non-negative integer`);
    }
    if (!Number.isInteger(credit) || credit < 0) {
      throw new TypeError(`lines[${i}].credit must be a non-negative integer`);
    }
    if ((debit > 0) === (credit > 0)) {
      // 両方 > 0 → 仕訳明細としては不正 (片側のみ非ゼロが原則)
      // 両方 0 → serializeLines で除外されるはずだが防御的に弾く
      throw new TypeError(
        `lines[${i}]: exactly one of debit/credit must be > 0 ` +
        `(got debit=${debit}, credit=${credit})`,
      );
    }
    totalDebit += debit;
    totalCredit += credit;
    normalizedLines.push({
      account_code: line.account_code,
      debit,
      credit,
      description: line.description ?? "",
    });
  }
  if (totalDebit !== totalCredit) {
    throw new TypeError(
      `unbalanced lines: debit=${totalDebit}, credit=${totalCredit}`,
    );
  }
  if (totalDebit === 0) {
    throw new TypeError("lines total must be > 0");
  }

  const entry = {
    date,
    description,
    source,
    fiscal_period: fiscalPeriod,
    lines: normalizedLines,
  };

  if (client !== undefined && client !== null) {
    _validateUserId(userId);
    return _encryptEntry(client, userId, entry);
  }
  return entry;
}


export function buildTransferEntry({
  client,
  userId,
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

  const entry = {
    date,
    description,
    source,
    fiscal_period: fiscalPeriod,
    lines: [
      { account_code: debitCode, debit: absAmount, credit: 0 },
      { account_code: creditCode, debit: 0, credit: absAmount },
    ],
  };

  if (client !== undefined && client !== null) {
    _validateUserId(userId);
    return _encryptEntry(client, userId, entry);
  }
  return entry;
}
