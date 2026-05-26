// Phase v5 BU-1: backup_export_client.js (decryptBackup) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/backup_export_client.js",
  import.meta.url,
);
const { decryptBackup } = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD, encryptRecord } = await import(REC.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode } = await import(B64.href);


// --- mock SharedCryptoClient (AAD round-trip) ---

function makeMockClient() {
  const aadStore = new Map();
  function key(b) { return Array.from(b.slice(0, 32)).join(","); }
  return {
    async encrypt(plaintext, aad) {
      const iv = new Uint8Array(12);
      crypto.getRandomValues(iv);
      const ciphertext = new Uint8Array(plaintext.length + 16);
      ciphertext.set(plaintext, 0);
      crypto.getRandomValues(ciphertext.subarray(plaintext.length));
      aadStore.set(key(ciphertext), new Uint8Array(aad || []));
      return { ciphertext, iv };
    },
    async decrypt(ciphertext, iv, aad) {
      const expected = aadStore.get(key(ciphertext));
      const actual = new Uint8Array(aad || []);
      if (!expected || expected.length !== actual.length) {
        throw new Error("decrypt: AAD mismatch");
      }
      for (let i = 0; i < expected.length; i++) {
        if (expected[i] !== actual[i]) throw new Error("decrypt: AAD mismatch");
      }
      return { plaintext: ciphertext.slice(0, ciphertext.length - 16) };
    },
  };
}


async function makeEncryptedRow(client, aad, body) {
  const { blob, iv } = await encryptRecord(client, body, aad);
  return {
    encrypted_blob: b64encode(blob),
    blob_iv: b64encode(iv),
  };
}


// --- argument validation ---

test("backup が object でないと TypeError", async () => {
  const client = makeMockClient();
  await assert.rejects(() => decryptBackup(client, null), /object/);
  await assert.rejects(() => decryptBackup(client, "foo"), /object/);
});

test("backup.data が object でないと TypeError", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => decryptBackup(client, { user_id: 1 }),
    /data/,
  );
});

test("backup.user_id が number でないと TypeError", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => decryptBackup(client, { user_id: "1", data: {} }),
    /user_id/,
  );
});


// --- shape ---

test("空 backup でも全テーブルキーが揃う", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    version: "1.0", user_id: 1, exported_at: "2026-05-27T00:00:00Z",
    data: {},
  });
  assert.equal(r.version, "1.0");
  assert.equal(r.user_id, 1);
  assert.deepEqual(r.data.accounts, []);
  assert.deepEqual(r.data.fiscal_closes, []);
  assert.deepEqual(r.data.journal_entries, []);
  assert.deepEqual(r.data.journal_entry_lines, []);
  assert.deepEqual(r.data.medical_expenses, []);
  assert.deepEqual(r.data.balance_cache_blobs, []);
});

test("accounts / fiscal_closes はそのまま通る (暗号化なし)", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      accounts: [{ code: "1010", name: "現金" }],
      fiscal_closes: [{ year: 2026, closed_period: 5 }],
    },
  });
  assert.deepEqual(r.data.accounts, [{ code: "1010", name: "現金" }]);
  assert.deepEqual(r.data.fiscal_closes, [{ year: 2026, closed_period: 5 }]);
});


// --- decryption: journal_entries ---

test("journal_entry: 復号成功で body が展開され、暗号文フィールドは削除", async () => {
  const client = makeMockClient();
  const userId = 7;
  const entryId = 42;
  const body = { date: "2026-03-15", description: "テスト仕訳", source: "journal" };
  const enc = await makeEncryptedRow(
    client, buildAAD("je", userId, entryId), body,
  );
  const r = await decryptBackup(client, {
    user_id: userId, data: {
      journal_entries: [{
        id: entryId, entry_number: 1, fiscal_year: 2026, ...enc,
      }],
    },
  });
  const row = r.data.journal_entries[0];
  assert.equal(row.id, entryId);
  assert.equal(row.date, "2026-03-15");
  assert.equal(row.description, "テスト仕訳");
  assert.equal(row.source, "journal");
  assert.equal(row.encrypted_blob, undefined);
  assert.equal(row.blob_iv, undefined);
});

test("journal_entry: 復号失敗で _decryptError がセットされる (他行は影響なし)", async () => {
  const client = makeMockClient();
  const userId = 7;
  const okEnc = await makeEncryptedRow(
    client, buildAAD("je", userId, 1), { date: "2026-03-15" },
  );
  // 別の userId 用に作った暗号文を userId=7 で復号しようとする → AAD 不一致
  const badEnc = await makeEncryptedRow(
    client, buildAAD("je", 999, 2), { date: "2026-04-01" },
  );
  const r = await decryptBackup(client, {
    user_id: userId, data: {
      journal_entries: [
        { id: 1, entry_number: 1, ...okEnc },
        { id: 2, entry_number: 2, ...badEnc },
      ],
    },
  });
  // 1 件目は復号成功
  assert.equal(r.data.journal_entries[0].date, "2026-03-15");
  // 2 件目は失敗で _decryptError あり
  assert.match(
    r.data.journal_entries[1]._decryptError, /AAD/,
  );
});

test("journal_entry: blob / iv なし (旧平文行) は body 展開なし", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      journal_entries: [{
        id: 5, entry_number: 5, date: "2025-12-31", description: "旧仕訳",
        encrypted_blob: null, blob_iv: null,
      }],
    },
  });
  const row = r.data.journal_entries[0];
  assert.equal(row.id, 5);
  assert.equal(row.date, "2025-12-31");  // 平文がそのまま
  assert.equal(row._decryptError, undefined);
});


// --- decryption: journal_entry_lines ---

test("journal_entry_line: AAD には entry_id + line_id 両方を含む", async () => {
  const client = makeMockClient();
  const userId = 3;
  const entryId = 100, lineId = 200;
  const body = { account_code: "5010", debit: 1000, credit: 0 };
  const enc = await makeEncryptedRow(
    client, buildAAD("jel", userId, entryId, lineId), body,
  );
  const r = await decryptBackup(client, {
    user_id: userId, data: {
      journal_entry_lines: [{
        id: lineId, journal_entry_id: entryId,
        account_code: "1010", debit_amount: 0, credit_amount: 0, ...enc,
      }],
    },
  });
  const row = r.data.journal_entry_lines[0];
  assert.equal(row.account_code, "5010");
  assert.equal(row.debit, 1000);
  // 平文の debit_amount は元の値のまま (body の debit と並存)
  assert.equal(row.debit_amount, 0);
});


// --- decryption: medical_expenses ---

test("medical_expense: 復号で body 展開", async () => {
  const client = makeMockClient();
  const userId = 9;
  const mid = 11;
  const body = {
    patient_name: "本人", hospital_name: "○病院",
    amount_paid: 5000, insurance_reimbursement: 1000,
  };
  const enc = await makeEncryptedRow(
    client, buildAAD("me", userId, mid), body,
  );
  const r = await decryptBackup(client, {
    user_id: userId, data: {
      medical_expenses: [{ id: mid, journal_entry_id: null, ...enc }],
    },
  });
  const row = r.data.medical_expenses[0];
  assert.equal(row.patient_name, "本人");
  assert.equal(row.amount_paid, 5000);
});


// --- decryption: balance_cache_blobs ---

test("balance_cache_blob: 復号で cumulative が展開される", async () => {
  const client = makeMockClient();
  const userId = 1;
  const year = 2026, period = 12;
  const body = { "1010": [100, 30], "5010": [5000, 0] };
  const enc = await makeEncryptedRow(
    client, buildAAD("bcb", userId, year * 100 + period), body,
  );
  const r = await decryptBackup(client, {
    user_id: userId, data: {
      balance_cache_blobs: [{
        year, period, updated_at: "2026-12-31T00:00:00Z", ...enc,
      }],
    },
  });
  const row = r.data.balance_cache_blobs[0];
  assert.equal(row.year, 2026);
  assert.equal(row.period, 12);
  assert.deepEqual(row.cumulative, { "1010": [100, 30], "5010": [5000, 0] });
  // 暗号文フィールドは含まれない
  assert.equal(row.encrypted_blob, undefined);
  assert.equal(row.blob_iv, undefined);
});

test("balance_cache_blob: 復号失敗で _decryptError、cumulative なし", async () => {
  const client = makeMockClient();
  const badEnc = await makeEncryptedRow(
    client, buildAAD("bcb", 999, 12), {},
  );
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      balance_cache_blobs: [{ year: 2026, period: 12, ...badEnc }],
    },
  });
  const row = r.data.balance_cache_blobs[0];
  assert.equal(row.cumulative, undefined);
  assert.match(row._decryptError, /AAD/);
});


// --- decryption: vouchers (passthrough, no decrypt needed) ---

test("vouchers: そのままパススルー (画像は復号不要)", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      vouchers: [
        {
          id: 7, image_key: "vouchers/x.jpg", image_mime: "image/jpeg",
          image_data: "base64string", file_hash: "abc",
        },
      ],
    },
  });
  assert.equal(r.data.vouchers.length, 1);
  assert.equal(r.data.vouchers[0].id, 7);
  assert.equal(r.data.vouchers[0].image_data, "base64string");
});

test("vouchers キーが backup.data に無くても空配列", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, { user_id: 1, data: {} });
  assert.deepEqual(r.data.vouchers, []);
});

test("vouchers: _imageError が付与されたデータもそのまま通る", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      vouchers: [
        {
          id: 8, image_key: "vouchers/gone.jpg",
          image_data: null, _imageError: "IOError: disk gone",
        },
      ],
    },
  });
  assert.equal(r.data.vouchers[0]._imageError, "IOError: disk gone");
  assert.equal(r.data.vouchers[0].image_data, null);
});


// --- BU-2b passthrough tables ---

test("ai_drafts: 配列がそのままパススルー", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      ai_drafts: [
        {
          id: 3, image_key: "drafts/p.jpg", image_mime: "image/jpeg",
          status: "pending", image_data: "abc",
        },
      ],
    },
  });
  assert.equal(r.data.ai_drafts.length, 1);
  assert.equal(r.data.ai_drafts[0].id, 3);
});

test("user_ai_config: 単一オブジェクトをパススルー", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      user_ai_config: { provider: "openai", api_key_blob: "xx" },
    },
  });
  assert.equal(r.data.user_ai_config.provider, "openai");
  assert.equal(r.data.user_ai_config.api_key_blob, "xx");
});

test("user_ai_config: 未設定で null", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, { user_id: 1, data: {} });
  assert.equal(r.data.user_ai_config, null);
});

test("webhook / csv / tax_mappings: 配列がパススルー", async () => {
  const client = makeMockClient();
  const r = await decryptBackup(client, {
    user_id: 1, data: {
      webhook_configs: [{ id: 1, webhook_url: "https://w" }],
      csv_column_profiles: [{ id: 2, account_code: "1010" }],
      tax_form_mappings: [{ id: 3, account_code: "1010", field_id: 5 }],
    },
  });
  assert.deepEqual(r.data.webhook_configs, [{ id: 1, webhook_url: "https://w" }]);
  assert.deepEqual(r.data.csv_column_profiles, [{ id: 2, account_code: "1010" }]);
  assert.deepEqual(r.data.tax_form_mappings, [
    { id: 3, account_code: "1010", field_id: 5 },
  ]);
});
