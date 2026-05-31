// edit_form_prefill.js (E3-F PR-D-6-3b-3) の単体テスト。
//
// applyEntryPrefill (純粋 DOM 反映) と hydrateEditForm (fetch+復号+skip 判定) を
// DI モックで検証する。DOM/Alpine は plain object で代替する。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/crypto/edit_form_prefill.js",
  import.meta.url,
);
const { applyEntryPrefill, hydrateEditForm } = await import(MOD.href);


// --- applyEntryPrefill ---

test("applyEntryPrefill: description input に値を設定", () => {
  const descInput = { value: "" };
  applyEntryPrefill({ fields: { date: null, description: "交通費" }, descInput });
  assert.equal(descInput.value, "交通費");
});

test("applyEntryPrefill: date は Alpine の dateValue を更新し checkDate を呼ぶ", () => {
  const dateInput = { value: "" };
  let checked = false;
  const scope = { dateValue: "", checkDate() { checked = true; } };
  const alpine = { $data: (el) => (el === dateInput ? scope : null) };
  applyEntryPrefill({
    fields: { date: "2026-06-15", description: "x" },
    dateInput, alpine,
  });
  assert.equal(dateInput.value, "2026-06-15");
  assert.equal(scope.dateValue, "2026-06-15");
  assert.equal(checked, true);
});

test("applyEntryPrefill: Alpine 無しでも input.value は更新される", () => {
  const dateInput = { value: "" };
  applyEntryPrefill({ fields: { date: "2026-01-02", description: "" }, dateInput });
  assert.equal(dateInput.value, "2026-01-02");
});

test("applyEntryPrefill: date が null なら date input を触らない", () => {
  const dateInput = { value: "keep" };
  applyEntryPrefill({ fields: { date: null, description: "x" }, dateInput });
  assert.equal(dateInput.value, "keep");
});

test("applyEntryPrefill: fields が null なら何もしない", () => {
  const descInput = { value: "keep" };
  applyEntryPrefill({ fields: null, descInput });
  assert.equal(descInput.value, "keep");
});


// --- hydrateEditForm ---

function makeClientClass(hasKey) {
  let closed = false;
  class C {
    constructor(_url) {}
    async status() { return { hasKey }; }
    async decrypt() { return {}; }
    close() { closed = true; }
    get _closed() { return closed; }
  }
  C._wasClosed = () => closed;
  return C;
}

test("hydrateEditForm: 新規 (isEdit=false) は skip", async () => {
  const r = await hydrateEditForm({ isEdit: false, entryId: 1, userId: 1 });
  assert.equal(r, null);
});

test("hydrateEditForm: 監査代理中は skip", async () => {
  const r = await hydrateEditForm({
    isEdit: true, entryId: 1, userId: 1, isProxyMode: true,
  });
  assert.equal(r, null);
});

test("hydrateEditForm: entryId 欠落は skip", async () => {
  const r = await hydrateEditForm({ isEdit: true, entryId: null, userId: 1 });
  assert.equal(r, null);
});

test("hydrateEditForm: MK ロック中 (hasKey=false) は復号せず null", async () => {
  const ClientClass = makeClientClass(false);
  let fetched = false;
  const r = await hydrateEditForm({
    isEdit: true, entryId: 1, userId: 1,
    ClientClass,
    fetchFields: async () => { fetched = true; return {}; },
  });
  assert.equal(r, null);
  assert.equal(fetched, false);
  assert.equal(ClientClass._wasClosed(), true);
});

test("hydrateEditForm: MK 解除済なら fetch+復号して反映", async () => {
  const ClientClass = makeClientClass(true);
  const dateInput = { value: "" };
  const descInput = { value: "" };
  const fields = { date: "2026-02-15", description: "JSON取得", fiscal_period: 2 };
  const r = await hydrateEditForm({
    isEdit: true, entryId: 42, userId: 9,
    dateInput, descInput,
    ClientClass,
    fetchFields: async ({ entryId, userId }) => {
      assert.equal(entryId, 42);
      assert.equal(userId, 9);
      return fields;
    },
  });
  assert.deepEqual(r, fields);
  assert.equal(dateInput.value, "2026-02-15");
  assert.equal(descInput.value, "JSON取得");
  assert.equal(ClientClass._wasClosed(), true);
});
