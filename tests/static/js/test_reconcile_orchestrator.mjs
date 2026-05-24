// E2 PR-C-6c: reconcile_orchestrator の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ORCH = new URL(
  "../../../app/static/js/crypto/reconcile_orchestrator.js",
  import.meta.url,
);
const {
  formatCsvRows, formatJournalRows, buildReconcilePrompt,
  filterMatches, runReconcile,
} = await import(ORCH.href);


function jsonResp(body, ok = true, status = 200) {
  return {
    ok, status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function makeFetch(routes) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({ url, init });
    for (const [pattern, handler] of routes) {
      if (typeof pattern === "string" && url === pattern) return handler(init);
      if (pattern instanceof RegExp && pattern.test(url)) return handler(init);
    }
    throw new Error(`mock: unhandled URL ${url}`);
  };
  fn.calls = calls;
  return fn;
}

function makeClient(decryptFn) {
  return { decrypt: decryptFn };
}


// ============ formatCsvRows / formatJournalRows ============

test("formatCsvRows: 番号 + 日付 + 説明 + 金額", () => {
  const t = formatCsvRows([
    { csv_index: 0, date: "2026-01-10", description: "アマゾン", amount: 1500 },
    { csv_index: 1, date: "2026-01-11", description: "セブン", amount: 500 },
  ]);
  assert.equal(
    t,
    "[0] 2026-01-10 アマゾン ¥1,500\n[1] 2026-01-11 セブン ¥500",
  );
});

test("formatJournalRows: ID + 日付 + 説明 + 金額 + カテゴリ", () => {
  const t = formatJournalRows([
    { entry_id: 99, date: "2026-01-10", description: "Amazon",
      amount: 1480, category_name: "日用品" },
  ]);
  assert.equal(t, "[ID:99] 2026-01-10 Amazon ¥1,480 (日用品)");
});


// ============ buildReconcilePrompt ============

test("buildReconcilePrompt: 2 プレースホルダ置換", () => {
  const p = buildReconcilePrompt({
    promptTemplate: "[C:__CSV_ROWS_TEXT__][J:__JOURNAL_ROWS_TEXT__]",
    csvRowsText: "csv-text",
    journalRowsText: "j-text",
  });
  assert.equal(p, "[C:csv-text][J:j-text]");
});

test("buildReconcilePrompt: promptTemplate 必須", () => {
  assert.throws(
    () => buildReconcilePrompt({ csvRowsText: "x", journalRowsText: "y" }),
    /promptTemplate is required/,
  );
});


// ============ filterMatches ============

test("filterMatches: confidence>=0.3 のみ", () => {
  const out = filterMatches({
    matches: [
      { csv_index: 0, entry_id: 1, confidence: 0.8, reason: "類似" },
      { csv_index: 1, entry_id: 2, confidence: 0.2, reason: "低" },
      { csv_index: 2, entry_id: null, confidence: 0, reason: "なし" },
    ],
  });
  assert.equal(out.length, 1);
  assert.equal(out[0].entry_id, 1);
  assert.equal(out[0].confidence, 0.8);
  assert.equal(out[0].reason, "類似");
});

test("filterMatches: 非 dict / matches 非配列で空", () => {
  assert.deepEqual(filterMatches(null), []);
  assert.deepEqual(filterMatches("string"), []);
  assert.deepEqual(filterMatches({}), []);
  assert.deepEqual(filterMatches({ matches: "not-array" }), []);
});

test("filterMatches: entry_id null は skip", () => {
  const out = filterMatches({
    matches: [
      { csv_index: 0, entry_id: null, confidence: 0.9 },
    ],
  });
  assert.deepEqual(out, []);
});

test("filterMatches: reason 欠落で空文字", () => {
  const out = filterMatches({
    matches: [{ csv_index: 0, entry_id: 1, confidence: 0.5 }],
  });
  assert.equal(out[0].reason, "");
});


// ============ runReconcile 統合 ============

const PROMPT_CTX = {
  prompt_template: "CSV:__CSV_ROWS_TEXT__\nJOURNAL:__JOURNAL_ROWS_TEXT__",
  batch_size: 30,
  unmatched_csv: [
    { csv_index: 0, date: "2026-01-10", description: "Amazon", amount: 1500 },
  ],
  journal_candidates: [
    { entry_id: 1, date: "2026-01-10", description: "アマゾン",
      amount: 1480, category_name: "日用品" },
  ],
  custom_prompt: "",
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


test("正常フロー: 単一バッチで matches 1 件返す", async () => {
  let llmCalls = 0;
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("sk-test"),
  }));
  const callLLMTextImpl = async () => {
    llmCalls++;
    return {
      result: {
        matches: [{
          csv_index: 0, entry_id: 1, confidence: 0.85, reason: "類似",
        }],
      },
      usage: {},
    };
  };
  const ret = await runReconcile({
    client, callLLMTextImpl, fetchImpl,
  });
  assert.equal(llmCalls, 1);
  assert.equal(ret.length, 1);
  assert.equal(ret[0].entry_id, 1);
  assert.equal(ret[0].confidence, 0.85);
});

test("バッチ処理: batch_size を超える unmatched で複数回 LLM 呼出", async () => {
  // batch_size=2 で 5 件 → 3 バッチ
  const big = [];
  for (let i = 0; i < 5; i++) {
    big.push({ csv_index: i, date: "2026-01-10",
                description: "x" + i, amount: 100 + i });
  }
  let llmCalls = 0;
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp({
      ...PROMPT_CTX, batch_size: 2, unmatched_csv: big,
    })],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  const ret = await runReconcile({
    client, fetchImpl,
    callLLMTextImpl: async () => {
      llmCalls++;
      return {
        result: { matches: [{
          csv_index: 0, entry_id: 1, confidence: 0.9,
        }]},
        usage: {},
      };
    },
  });
  assert.equal(llmCalls, 3); // 5/2 = 2.5 → 3 バッチ
  // 各バッチで 1 件返るので合計 3 件
  assert.equal(ret.length, 3);
});

test("unmatched_csv 空 → LLM 呼ばずに空配列", async () => {
  let llmCalls = 0;
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp({
      ...PROMPT_CTX, unmatched_csv: [],
    })],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  const ret = await runReconcile({
    client, fetchImpl,
    callLLMTextImpl: async () => { llmCalls++; return { result: {} }; },
  });
  assert.deepEqual(ret, []);
  assert.equal(llmCalls, 0);
});

test("journal_candidates 空 → LLM 呼ばずに空配列", async () => {
  let llmCalls = 0;
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp({
      ...PROMPT_CTX, journal_candidates: [],
    })],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  const ret = await runReconcile({
    client, fetchImpl,
    callLLMTextImpl: async () => { llmCalls++; return { result: {} }; },
  });
  assert.deepEqual(ret, []);
  assert.equal(llmCalls, 0);
});

test("非 E2EE config で throw", async () => {
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", is_e2ee: false,
      api_key_blob: null, api_key_iv: null,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  await assert.rejects(
    () => runReconcile({
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /E2EE 形式ではありません/,
  );
});

test("model_name 空ならデフォルトモデル使用", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "anthropic", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await runReconcile({
    client, fetchImpl,
    callLLMTextImpl: async (args) => {
      llmArgs = args;
      return { result: { matches: [] } };
    },
  });
  assert.equal(llmArgs.model, "claude-sonnet-4-20250514");
});

test("未対応 provider で throw", async () => {
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "evil", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => runReconcile({
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /unsupported provider/,
  );
});

test("非 dict 応答は空配列扱い (フィルタで吸収)", async () => {
  const fetchImpl = makeFetch([
    ["/csv-import/ai-reconcile-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  const ret = await runReconcile({
    client, fetchImpl,
    callLLMTextImpl: async () => ({ result: "unexpected" }),
  });
  assert.deepEqual(ret, []);
});

test("必須引数欠如で throw", async () => {
  await assert.rejects(
    () => runReconcile({}),
    /client.*is required/,
  );
});
