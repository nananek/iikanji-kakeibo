// E5 #112 PR-E: 監査スナップショット生成 (audit_snapshot.js) の単体テスト。
// 既存の集計・取得関数の組み立てを、モック fetch + モック client で検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL("../../../app/static/js/crypto/audit_snapshot.js", import.meta.url);
const { buildSnapshotLv1, buildSnapshotLv2, buildSnapshotLv3 } = await import(M.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode } = await import(B64.href);

const TE = new TextEncoder();
const b64json = (obj) => b64encode(TE.encode(JSON.stringify(obj)));

// client.decrypt は identity (plaintext = blob) → decryptRecord が JSON.parse。
// #338 item4: API は line 平文 (account_code/debit/credit) を返さなくなったため、
// line も encrypted_blob を持たせて _normalizeLine が復号する形に統一する。
const identityClient = { decrypt: async (blob) => ({ plaintext: blob }) };

const _iv = b64encode(new Uint8Array(12));
// 復号 (identity) すると body になる line を作る。_normalizeLine が
// encrypted_blob を b64decode → identity → JSON.parse して body.account_code 等を得る。
function encLine(id, account_code, debit, credit) {
  return {
    id,
    encrypted_blob: b64json({ account_code, debit_amount: debit, credit_amount: credit }),
    blob_iv: _iv,
  };
}

const ACCOUNTS_META = {
  "1010": { type: "asset", normal_balance: "debit", name: "現金", tax_category: null },
  "4010": { type: "revenue", normal_balance: "credit", name: "売上", tax_category: null },
  "5200": { type: "expense", normal_balance: "debit", name: "社保", tax_category: "social_insurance" },
};

// 仕訳: entry1 = 税務科目なし, entry2 = 社保 (税務科目) を含む。
const ENTRIES = [
  {
    id: 1, fiscal_year: 2026, fiscal_month: 3, is_closing: false,
    lines: [
      encLine(101, "1010", 1000, 0),
      encLine(102, "4010", 0, 1000),
    ],
  },
  {
    id: 2, fiscal_year: 2026, fiscal_month: 5, is_closing: false,
    lines: [
      encLine(201, "5200", 500, 0),
      encLine(202, "1010", 0, 500),
    ],
  },
];

function reportFetch() {
  // /api/v1/journals?... → page 1 に全件、page>=2 は空 (ページングを確実に打ち切る)。
  // /api/v1/balance-cache-blobs?... → 空 (priorCumulative={})。
  return async (url) => {
    if (url.startsWith("/api/v1/journals")) {
      const page = Number(new URL(url, "http://x").searchParams.get("page"));
      return {
        ok: true, status: 200,
        async json() { return { journals: page === 1 ? ENTRIES : [] }; },
      };
    }
    if (url.startsWith("/api/v1/balance-cache-blobs")) {
      return { ok: true, status: 200, async json() { return { blobs: [] }; } };
    }
    throw new Error("unexpected url " + url);
  };
}


// ===== Lv1 =====

test("buildSnapshotLv1 returns aggregates only, no raw entries", async () => {
  const snap = await buildSnapshotLv1({
    client: identityClient, userId: 7, fiscalYear: 2026,
    accountsMeta: ACCOUNTS_META, fetchImpl: reportFetch(),
  });
  assert.equal(snap.v, 1);
  assert.equal(snap.level, 1);
  assert.equal(snap.fiscal_year, 2026);
  assert.deepEqual(snap.accounts_meta, ACCOUNTS_META);
  assert.ok(Array.isArray(snap.trial_balance));
  assert.ok(snap.profit_loss && snap.balance_sheet && snap.monthly);
  // 仕訳本体は含めない
  assert.equal(snap.entries, undefined);
  // 試算表に現金(1010)が集計されている
  assert.ok(snap.trial_balance.some((r) => r.account_code === "1010" && r.debit === 1000));
});


// ===== Lv2 =====

test("buildSnapshotLv2 includes only tax-category entries + tax_summary", async () => {
  const snap = await buildSnapshotLv2({
    client: identityClient, userId: 7, fiscalYear: 2026,
    accountsMeta: ACCOUNTS_META, fetchImpl: reportFetch(),
  });
  assert.equal(snap.level, 2);
  assert.ok(snap.tax_summary, "has tax_summary");
  // entry2 (社保 5200 を含む) のみ。entry1 は税務科目なしなので除外。
  assert.equal(snap.entries.length, 1);
  assert.equal(snap.entries[0].id, 2);
  // Lv1 と同じ集計も持つ
  assert.ok(Array.isArray(snap.trial_balance));
});


// ===== Lv3 =====

function backupFetch() {
  const backup = {
    version: "1.0", exported_at: "2026-06-02T00:00:00Z", user_id: 7,
    data: {
      accounts: [{ code: "1010", name: "現金" }],
      fiscal_closes: [{ year: 2026, closed_period: 3 }],
      journal_entries: [
        { id: 1, blob_iv: b64encode(new Uint8Array(12)),
          encrypted_blob: b64json({ date: "2026-01-01", description: "テスト" }) },
      ],
      journal_entry_lines: [
        { id: 10, journal_entry_id: 1, blob_iv: b64encode(new Uint8Array(12)),
          encrypted_blob: b64json({ account_code: "1010", debit_amount: 100, credit_amount: 0 }) },
      ],
      medical_expenses: [],
      balance_cache_blobs: [],
      // voucher 5 = 復号可能な PNG (aad_id あり)、voucher 6 = aad_id 無し → _imageError。
      vouchers: [
        { id: 5, aad_id: 99, journal_entry_id: 1, file_hash: "abc",
          image_data: b64encode(VOUCHER_BLOB) },
        { id: 6, image_data: "ZmFrZQ==", encrypted_meta_blob: "x" },
      ],
      user_ai_config: { api_key_blob: "secret" },
    },
  };
  return async (url) => {
    if (url === "/api/v1/backup/export") {
      return { ok: true, status: 200, async json() { return backup; } };
    }
    throw new Error("unexpected url " + url);
  };
}

// iv(12B 0埋め) || ct(PNG マジック + padding)。identityClient は ct を平文として返す。
const PNG_CT = new Uint8Array(16);
PNG_CT.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 0);
const VOUCHER_BLOB = new Uint8Array(12 + PNG_CT.length);
VOUCHER_BLOB.set(PNG_CT, 12);

test("buildSnapshotLv3 decrypts ledger, excludes settings, inlines vouchers", async () => {
  const snap = await buildSnapshotLv3({
    client: identityClient, userId: 7,
    accountsMeta: ACCOUNTS_META, fetchImpl: backupFetch(),
  });
  assert.equal(snap.level, 3);
  // 台帳は復号されている
  assert.equal(snap.journal_entries.length, 1);
  assert.equal(snap.journal_entries[0].date, "2026-01-01");
  assert.equal(snap.journal_entry_lines[0].account_code, "1010");
  assert.equal(snap.journal_entry_lines[0].debit, undefined); // body は debit_amount キー
  assert.equal(snap.journal_entry_lines[0].debit_amount, 100);
  assert.deepEqual(snap.accounts, [{ code: "1010", name: "現金" }]);
  // 設定系は含めない
  assert.equal(snap.user_ai_config, undefined);
  // 証憑は inline base64 で同梱 (§14.6)
  assert.equal(snap.vouchers.length, 2);
  const v5 = snap.vouchers.find((v) => v.voucher_id === 5);
  assert.equal(v5.mime, "image/png");
  assert.equal(v5.journal_entry_id, 1);
  assert.equal(v5.aad_id, 99);
  assert.equal(v5.image_base64, b64encode(PNG_CT));
  assert.ok(!v5._imageError);
  // aad_id 無しは復号できず _imageError で局所スキップ
  const v6 = snap.vouchers.find((v) => v.voucher_id === 6);
  assert.equal(v6._imageError, true);
  assert.equal(v6.image_base64, undefined);
});

test("buildSnapshotLv3 throws on backup export HTTP error", async () => {
  const fetchImpl = async () => ({ ok: false, status: 500, async json() { return {}; } });
  await assert.rejects(
    () => buildSnapshotLv3({ client: identityClient, userId: 7, accountsMeta: {}, fetchImpl }),
    /backup export HTTP 500/,
  );
});
