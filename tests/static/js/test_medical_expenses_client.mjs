// medical_expenses_client.js (Phase E3-C-8b) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/medical_expenses_client.js",
  import.meta.url,
);
const { fetchMedicalExpensesForYear } = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode } = await import(B64.href);


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


async function makeEncryptedExpense(client, userId, expenseId, body) {
  // E3-F PR-A: AAD は Option B (user_id のみ)。
  const aad = buildAAD("me", userId);
  const pt = new TextEncoder().encode(JSON.stringify(body));
  const { ciphertext, iv } = await client.encrypt(pt, aad);
  return {
    id: expenseId,
    journal_entry_id: 999,
    date: null, patient_name: "", hospital_name: "",
    treatment_description: "", provider_type: "",
    amount_paid: 0, insurance_reimbursement: 0,
    encrypted_blob: b64encode(ciphertext),
    blob_iv: b64encode(iv),
  };
}

function makeFetch(expenses) {
  return async (url) => {
    return {
      ok: true,
      json: async () => ({ ok: true, expenses, total: expenses.length }),
    };
  };
}


// --- tests ---

test("argument validation", async () => {
  await assert.rejects(() => fetchMedicalExpensesForYear({ userId: 1, fiscalYear: 2026 }), /client.*required/);
  const client = makeMockClient();
  await assert.rejects(() => fetchMedicalExpensesForYear({ client, fiscalYear: 2026 }), /userId is required/);
  await assert.rejects(() => fetchMedicalExpensesForYear({ client, userId: "abc", fiscalYear: 2026 }), /number or bigint/);
  await assert.rejects(() => fetchMedicalExpensesForYear({ client, userId: 1, fiscalYear: 999 }), /1900\.\.2200/);
});

test("空 list で空 array", async () => {
  const client = makeMockClient();
  const r = await fetchMedicalExpensesForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl: makeFetch([]),
  });
  assert.deepEqual(r, []);
});

test("HTTP エラー時 throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({
    ok: false, status: 500, json: async () => ({ error: "Internal" }),
  });
  await assert.rejects(
    () => fetchMedicalExpensesForYear({ client, userId: 1, fiscalYear: 2026, fetchImpl }),
    /HTTP 500/,
  );
});

test("dual-read: blob/iv null は平文フォールバック", async () => {
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 1, journal_entry_id: 100,
    date: "2026-05-01", patient_name: "本人", hospital_name: "A",
    treatment_description: "風邪", provider_type: "hospital",
    amount_paid: 3000, insurance_reimbursement: 1000,
    encrypted_blob: null, blob_iv: null,
  }]);
  const r = await fetchMedicalExpensesForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(r.length, 1);
  assert.equal(r[0].patient_name, "本人");
  assert.equal(r[0].amount_paid, 3000);
  assert.equal(r[0].insurance_reimbursement, 1000);
});

test("encrypted: AAD 込み round-trip 復号", async () => {
  const client = makeMockClient();
  const userId = 1;
  const exp = await makeEncryptedExpense(client, userId, 50, {
    v: 1,
    date: "2026-05-15",
    patient_name: "家族",
    hospital_name: "B病院",
    treatment_description: "歯科",
    provider_type: "hospital",
    amount_paid: 8000,
    insurance_reimbursement: 2000,
  });
  const r = await fetchMedicalExpensesForYear({
    client, userId, fiscalYear: 2026, fetchImpl: makeFetch([exp]),
  });
  assert.equal(r[0].patient_name, "家族");
  assert.equal(r[0].hospital_name, "B病院");
  assert.equal(r[0].provider_type, "hospital");
  assert.equal(r[0].amount_paid, 8000);
  assert.equal(r[0].insurance_reimbursement, 2000);
});

test("AAD すり替え (別 user_id) は平文フォールバック (全件 reject せず)", async () => {
  const client = makeMockClient();
  const exp = await makeEncryptedExpense(client, 1, 60, {
    patient_name: "暗号化済", amount_paid: 9999,
  });
  // 平文 fields は null/空のまま (本来 dual-read の平文があるはずだが
  // 暗号化テスト用)
  const r = await fetchMedicalExpensesForYear({
    client, userId: 2, fiscalYear: 2026, fetchImpl: makeFetch([exp]),
    // userId mismatch
  });
  assert.equal(r.length, 1);
  // 復号失敗で body=null、平文 fallback も空なのでデフォルトに
  assert.equal(r[0].patient_name, "");
  assert.equal(r[0].amount_paid, 0);
});

test("複数 expense", async () => {
  const client = makeMockClient();
  const fetchImpl = makeFetch([
    {id: 1, journal_entry_id: 10, patient_name: "本人", hospital_name: "A",
     amount_paid: 1000, insurance_reimbursement: 0, encrypted_blob: null,
     blob_iv: null, date: "2026-01-01", provider_type: "hospital",
     treatment_description: ""},
    {id: 2, journal_entry_id: 20, patient_name: "家族", hospital_name: "B",
     amount_paid: 2000, insurance_reimbursement: 500, encrypted_blob: null,
     blob_iv: null, date: "2026-02-01", provider_type: "pharmacy",
     treatment_description: ""},
  ]);
  const r = await fetchMedicalExpensesForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(r.length, 2);
  assert.equal(r[0].patient_name, "本人");
  assert.equal(r[1].patient_name, "家族");
});
