// 監査スナップショット送信 UI の純粋ロジック (E5 #112 / §14.7)。
// DOM/IndexedDB 非依存の computeNextRound / unacknowledgedForGrant を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/audit/packages_renderer.mjs",
  import.meta.url,
);
const { computeNextRound, unacknowledgedForGrant } = await import(M.href);

test("computeNextRound: 空なら 1", () => {
  assert.equal(computeNextRound([]), 1);
  assert.equal(computeNextRound(undefined), 1);
});

test("computeNextRound: 最大 round + 1", () => {
  assert.equal(computeNextRound([{ round_id: 1 }, { round_id: 3 }, { round_id: 2 }]), 4);
  assert.equal(computeNextRound([{ round_id: 5 }]), 6);
});

test("computeNextRound: 不正な round_id は無視", () => {
  assert.equal(computeNextRound([{ round_id: 2 }, { round_id: null }, {}]), 3);
});

test("unacknowledgedForGrant: この grant の未確認 response のみ", () => {
  const responses = [
    { audit_package_id: 10, owner_acknowledged_at: null },      // 対象・未確認
    { audit_package_id: 10, owner_acknowledged_at: "2026-06-01" }, // 対象・確認済
    { audit_package_id: 99, owner_acknowledged_at: null },      // 他 grant
  ];
  const pending = unacknowledgedForGrant(responses, [10, 11]);
  assert.equal(pending.length, 1);
  assert.equal(pending[0].audit_package_id, 10);
  assert.equal(pending[0].owner_acknowledged_at, null);
});

test("unacknowledgedForGrant: パッケージ無しなら空", () => {
  assert.deepEqual(
    unacknowledgedForGrant([{ audit_package_id: 1, owner_acknowledged_at: null }], []),
    [],
  );
  assert.deepEqual(unacknowledgedForGrant(undefined, [1]), []);
});
