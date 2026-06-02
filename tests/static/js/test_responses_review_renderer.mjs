// owner 側 修正案レビュー (responses_review_renderer) の純粋ロジック (E5 #112 / §14.9)。
// DOM/crypto 非依存の responsesForGrant / parseResponse を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/audit/responses_review_renderer.mjs",
  import.meta.url,
);
const { responsesForGrant, parseResponse } = await import(M.href);

test("responsesForGrant: この grant のパッケージの response のみ", () => {
  const responses = [
    { id: 1, audit_package_id: 10, created_at: "2026-06-01T00:00:00Z" },
    { id: 2, audit_package_id: 99, created_at: "2026-06-02T00:00:00Z" }, // 他 grant
    { id: 3, audit_package_id: 11, created_at: "2026-06-03T00:00:00Z" },
  ];
  const out = responsesForGrant(responses, [10, 11]);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((r) => r.id), [3, 1]); // created_at 降順
});

test("responsesForGrant: パッケージ無しなら空", () => {
  assert.deepEqual(
    responsesForGrant([{ id: 1, audit_package_id: 1 }], []),
    [],
  );
  assert.deepEqual(responsesForGrant(undefined, [1]), []);
});

test("responsesForGrant: created_at 欠落でも安定", () => {
  const responses = [
    { id: 1, audit_package_id: 10 },
    { id: 2, audit_package_id: 10, created_at: "2026-06-02T00:00:00Z" },
  ];
  const out = responsesForGrant(responses, [10]);
  assert.equal(out.length, 2);
  // created_at ありが先頭 (降順)
  assert.equal(out[0].id, 2);
});

test("parseResponse: バイト列を JSON へ復元", () => {
  const payload = {
    v: 1,
    response_type: "revision",
    summary: "所見",
    comments: [{ entry_id: 5, note: "確認" }],
  };
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  assert.deepEqual(parseResponse(bytes), payload);
});
