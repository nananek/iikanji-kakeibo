// 監査スナップショット生成 (E5 #112 / 設計書 §14.2.1)。
//
// owner がクライアントで Lv1/Lv2/Lv3 の **平文スナップショット JSON** を組み立てる。
// HPKE seal (audit_hpke.sealAuditPackage) と送信は送信 UI (次ラウンド) で結線する。
// 本モジュールは既存のレポート集計 (crypto/reports/*.js) とデータ取得・復号
// クライアントを再利用するだけで、新規の暗号ロジックは持たない。
//
// スナップショットは auditor のクライアントが accounts_meta を使って描画する想定。
//   Lv1: 集計のみ (試算表 / P/L / B/S / 月次比較)。仕訳本体は含めない。
//   Lv2: Lv1 + 税務科目フィルタ済みの仕訳 + 税務集計 (owner 側で強制フィルタ, §14.5)。
//   Lv3: 全データ (本人同等)。decryptBackup で台帳全体を復号して同梱する。
//        証憑画像の同梱 (サイズ / 別添方式) は送信 UI ラウンドで再検討するため本 PR では未同梱。

import { fetchJournalsForYear } from "./journals_client.js";
import { fetchBalanceCacheBlobs } from "./balance_cache_blobs_client.js";
import { computeTrialBalance } from "./reports/trial_balance.js";
import { computeProfitLoss } from "./reports/profit_loss.js";
import { computeBalanceSheet } from "./reports/balance_sheet.js";
import { computeMonthlyComparison } from "./reports/monthly_comparison.js";
import { computeTaxSummary } from "./reports/tax_summary.js";
import { decryptBackup } from "./backup_export_client.js";

const SNAPSHOT_VERSION = 1;

/** accounts_meta (code → {type, normal_balance, name, tax_category}) から compute 用の by-code マップを導出。 */
function _maps(accountsMeta) {
  const accountTypeByCode = {};
  const normalBalanceByCode = {};
  const accountNameByCode = {};
  const taxCategoryByCode = {};
  for (const [code, meta] of Object.entries(accountsMeta || {})) {
    accountTypeByCode[code] = meta.type;
    normalBalanceByCode[code] = meta.normal_balance;
    accountNameByCode[code] = meta.name;
    taxCategoryByCode[code] = meta.tax_category ?? null;
  }
  return { accountTypeByCode, normalBalanceByCode, accountNameByCode, taxCategoryByCode };
}

/**
 * 指定年度の仕訳を取得し、レポート集計 (試算表 / P/L / B/S / 月次比較) を計算する。
 * B/S は balance_sheet_renderer と同じく前年末 BCB(period=15) を priorCumulative に流す。
 * @returns {Promise<{entries, trial_balance, profit_loss, balance_sheet, monthly}>}
 */
async function _yearReports(client, userId, fiscalYear, accountsMeta, fetchImpl) {
  const { accountTypeByCode, normalBalanceByCode, accountNameByCode } = _maps(accountsMeta);
  // 前年末累計 (priorCumulative) を当年 entries と並列取得。前年 BCB 欠落時は {} で degraded。
  const bcbPromise = fetchBalanceCacheBlobs({
    client, userId, fiscalYear: fiscalYear - 1, fetchImpl,
  }).catch(() => ({}));
  const journalsPromise = fetchJournalsForYear({ client, userId, fiscalYear, fetchImpl });
  const [blobs, entries] = await Promise.all([bcbPromise, journalsPromise]);
  const priorCumulative = blobs[15] || {};

  return {
    entries,
    trial_balance: computeTrialBalance(entries, {}),
    profit_loss: computeProfitLoss(entries, { accountTypeByCode, accountNameByCode }),
    balance_sheet: computeBalanceSheet(entries, {
      accountTypeByCode, normalBalanceByCode, accountNameByCode, priorCumulative,
    }),
    monthly: computeMonthlyComparison(entries, { accountTypeByCode, accountNameByCode }),
  };
}

/**
 * Lv1 (集計のみ) スナップショット。仕訳本体は含めない。
 * @param {Object} a
 * @param {Object} a.client       SharedCryptoClient
 * @param {number} a.userId
 * @param {number} a.fiscalYear
 * @param {Object} a.accountsMeta code → {type, normal_balance, name, tax_category}
 * @param {typeof fetch} [a.fetchImpl]
 */
export async function buildSnapshotLv1({ client, userId, fiscalYear, accountsMeta, fetchImpl }) {
  const r = await _yearReports(client, userId, fiscalYear, accountsMeta, fetchImpl);
  return {
    v: SNAPSHOT_VERSION,
    level: 1,
    fiscal_year: fiscalYear,
    accounts_meta: accountsMeta,
    trial_balance: r.trial_balance,
    profit_loss: r.profit_loss,
    balance_sheet: r.balance_sheet,
    monthly: r.monthly,
  };
}

/**
 * Lv2 (税務科目限定) スナップショット。Lv1 + 税務科目を持つ仕訳のみ + 税務集計。
 * フィルタは owner クライアント側で強制する (§14.5、E2EE と矛盾しない)。
 */
export async function buildSnapshotLv2({ client, userId, fiscalYear, accountsMeta, fetchImpl }) {
  const { taxCategoryByCode, accountNameByCode } = _maps(accountsMeta);
  const r = await _yearReports(client, userId, fiscalYear, accountsMeta, fetchImpl);
  // 税務科目 (tax_category != null) に該当する行を 1 つでも含む仕訳のみ抽出。
  const taxEntries = r.entries.filter((e) =>
    (e.lines || []).some(
      (l) => l.account_code != null && taxCategoryByCode[l.account_code] != null,
    ),
  );
  return {
    v: SNAPSHOT_VERSION,
    level: 2,
    fiscal_year: fiscalYear,
    accounts_meta: accountsMeta,
    trial_balance: r.trial_balance,
    profit_loss: r.profit_loss,
    balance_sheet: r.balance_sheet,
    monthly: r.monthly,
    tax_summary: computeTaxSummary(r.entries, { taxCategoryByCode, accountNameByCode }),
    entries: taxEntries,
  };
}

/**
 * Lv3 (本人同等) スナップショット。全台帳を decryptBackup で復号して同梱する。
 * /api/v1/backup/export はサーバが暗号文のまま返し、decryptBackup が本人 MK で復号する。
 * 設定系 (user_ai_config / webhook 等) は監査に不要なので除外する。
 * 証憑画像は本 PR では未同梱 (サイズ / 別添方式は送信 UI ラウンドで決める)。
 */
export async function buildSnapshotLv3({ client, accountsMeta, fetchImpl }) {
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f("/api/v1/backup/export", { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`buildSnapshotLv3: backup export HTTP ${r.status} ${e.error || ""}`);
  }
  const backup = await r.json();
  const decrypted = await decryptBackup(client, backup);
  const d = decrypted.data || {};
  return {
    v: SNAPSHOT_VERSION,
    level: 3,
    accounts_meta: accountsMeta,
    accounts: d.accounts || [],
    fiscal_closes: d.fiscal_closes || [],
    journal_entries: d.journal_entries || [],
    journal_entry_lines: d.journal_entry_lines || [],
    medical_expenses: d.medical_expenses || [],
    balance_cache_blobs: d.balance_cache_blobs || [],
    // vouchers: 証憑画像同梱は送信 UI ラウンドで対応 (本 PR では未同梱)。
  };
}
