// medical_expense_builder.js (Phase E3-F PR-D-3) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/medical_expense_builder.js",
  import.meta.url,
);
const { buildMedicalExpense } = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD, decryptRecord } = await import(REC.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64decode } = await import(B64.href);


// --- mock SharedCryptoClient (AAD 込みの round-trip 検証可能) ---

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
      const plen = ciphertext.length - 16;
      return { plaintext: ciphertext.slice(0, plen) };
    },
  };
}


// --- 平文 (client なし) ---

test("client なしで平文 payload を返す", () => {
  const p = buildMedicalExpense({
    journalEntryId: 42,
    date: "2026-05-01",
    patientName: "本人",
    hospitalName: "A病院",
    treatmentDescription: "内科",
    providerType: "hospital",
    amountPaid: 5000,
    insuranceReimbursement: 1000,
  });
  assert.equal(p.journal_entry_id, 42);
  assert.equal(p.date, "2026-05-01");
  assert.equal(p.patient_name, "本人");
  assert.equal(p.hospital_name, "A病院");
  assert.equal(p.treatment_description, "内科");
  assert.equal(p.provider_type, "hospital");
  assert.equal(p.amount_paid, 5000);
  assert.equal(p.insurance_reimbursement, 1000);
  assert.equal(p.encrypted_blob, undefined);
});

test("provider_type 空文字 → null に正規化、未指定はデフォルト", () => {
  const p = buildMedicalExpense({ journalEntryId: 1, providerType: "" });
  assert.equal(p.provider_type, null);
  assert.equal(p.patient_name, "");
  assert.equal(p.amount_paid, 0);
  assert.equal(p.insurance_reimbursement, 0);
  assert.equal(p.date, null);
});

test("journalEntryId 必須", () => {
  assert.throws(() => buildMedicalExpense({}), /journalEntryId is required/);
});

test("負の金額は拒否", () => {
  assert.throws(
    () => buildMedicalExpense({ journalEntryId: 1, amountPaid: -1 }),
    /non-negative integer/,
  );
  assert.throws(
    () => buildMedicalExpense({ journalEntryId: 1, insuranceReimbursement: 1.5 }),
    /non-negative integer/,
  );
});


// --- 暗号化 (client + userId) ---

test("encrypted: encrypted_blob / blob_iv が付き、round-trip で復号できる", async () => {
  const client = makeMockClient();
  const userId = 7;
  const p = await buildMedicalExpense({
    client, userId,
    journalEntryId: 42,
    date: "2026-05-01",
    patientName: "家族",
    hospitalName: "B薬局",
    treatmentDescription: "処方薬",
    providerType: "pharmacy",
    amountPaid: 8000,
    insuranceReimbursement: 2000,
  });
  assert.ok(typeof p.encrypted_blob === "string" && p.encrypted_blob.length > 0);
  assert.ok(typeof p.blob_iv === "string" && p.blob_iv.length > 0);
  assert.equal(p.journal_entry_id, 42);
  // E3-F PR-D-6-6: 平文は wire に乗らない (encrypted_blob 内のみ)。
  assert.equal(p.patient_name, undefined);
  assert.equal(p.hospital_name, undefined);
  assert.equal(p.treatment_description, undefined);
  assert.equal(p.provider_type, undefined);
  assert.equal(p.amount_paid, undefined);
  assert.equal(p.insurance_reimbursement, undefined);
  assert.equal(p.date, undefined);

  const aad = buildAAD("me", userId);
  const body = await decryptRecord(
    client, b64decode(p.encrypted_blob), b64decode(p.blob_iv), aad,
  );
  assert.equal(body.v, 1);
  assert.equal(body.patient_name, "家族");
  assert.equal(body.hospital_name, "B薬局");
  assert.equal(body.provider_type, "pharmacy");
  assert.equal(body.amount_paid, 8000);
  assert.equal(body.insurance_reimbursement, 2000);
});

test("encrypted: 別 userId の AAD では復号失敗 (swap 検知)", async () => {
  const client = makeMockClient();
  const p = await buildMedicalExpense({
    client, userId: 7, journalEntryId: 1, amountPaid: 100,
  });
  const wrongAad = buildAAD("me", 8);
  await assert.rejects(
    () => decryptRecord(client, b64decode(p.encrypted_blob), b64decode(p.blob_iv), wrongAad),
    /AAD mismatch/,
  );
});

test("encrypted: client 指定時に userId 不正なら throw", async () => {
  const client = makeMockClient();
  assert.throws(
    () => buildMedicalExpense({ client, userId: "x", journalEntryId: 1 }),
    /number or bigint/,
  );
});
