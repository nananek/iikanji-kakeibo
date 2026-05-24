// E2 PR-C-6b: suggest_categories_orchestrator の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ORCH = new URL(
  "../../../app/static/js/crypto/suggest_categories_orchestrator.js",
  import.meta.url,
);
const {
  buildRowsText, buildSuggestCategoriesPrompt, normalizeSuggestions,
  runSuggestCategories,
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

const PROMPT_CTX = {
  prompt_template:
    "口座:__PAYMENT_ACCOUNT_NAME__\n元帳:\n__LEDGER_CONTEXT__\n科目:\n__ACCOUNT_LIST__\n取引:\n__ROWS_TEXT__",
  payment_account_name: "現金",
  ledger_context: "(元帳サンプル)",
  account_list: "5010 食費\n1010 現金",
  account_map: { "5010": "食費", "1010": "現金" },
  custom_prompt: "",
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


// ============ buildRowsText ============

test("buildRowsText: 番号付き整形", () => {
  const t = buildRowsText([
    { description: "セブン", deposit: 0, withdrawal: 500 },
    { description: "給与", deposit: 250000, withdrawal: 0 },
  ]);
  assert.equal(
    t,
    "0. セブン (入金: ¥0, 出金: ¥500)\n"
    + "1. 給与 (入金: ¥250,000, 出金: ¥0)",
  );
});

test("buildRowsText: 非配列で throw", () => {
  assert.throws(() => buildRowsText(null), /rows must be an array/);
});

test("buildRowsText: 欠損は空文字 / 0", () => {
  const t = buildRowsText([{}]);
  assert.equal(t, "0.  (入金: ¥0, 出金: ¥0)");
});


// ============ buildSuggestCategoriesPrompt ============

test("buildSuggestCategoriesPrompt: 4 プレースホルダを置換", () => {
  const p = buildSuggestCategoriesPrompt({
    promptTemplate: "[A:__PAYMENT_ACCOUNT_NAME__][L:__LEDGER_CONTEXT__]"
      + "[C:__ACCOUNT_LIST__][R:__ROWS_TEXT__]",
    paymentAccountName: "現金",
    ledgerContext: "L1",
    accountList: "A1",
    rowsText: "R1",
  });
  assert.equal(p, "[A:現金][L:L1][C:A1][R:R1]");
});

test("buildSuggestCategoriesPrompt: promptTemplate 欠如で throw", () => {
  assert.throws(
    () => buildSuggestCategoriesPrompt({}),
    /promptTemplate is required/,
  );
});


// ============ normalizeSuggestions ============

test("normalizeSuggestions: account_map で code → name 解決", () => {
  const out = normalizeSuggestions(
    { results: [
      { index: 0, account_code: "5010" },
      { index: 1, account_code: "1010" },
    ]},
    [{ description: "セブン" }, { description: "ATM" }],
    { "5010": "食費", "1010": "現金" },
  );
  assert.deepEqual(out, {
    "セブン": { account_code: "5010", account_name: "食費" },
    "ATM": { account_code: "1010", account_name: "現金" },
  });
});

test("normalizeSuggestions: 範囲外 index / null code / 重複描写 を skip", () => {
  const out = normalizeSuggestions(
    { results: [
      { index: 99, account_code: "5010" },     // 範囲外
      { index: null, account_code: "5010" },   // index null
      { index: 0, account_code: null },        // code null
      { index: 0, account_code: "5010" },      // 正常
      { index: 0, account_code: "1010" },      // 重複 desc (skip)
    ]},
    [{ description: "セブン" }],
    { "5010": "食費", "1010": "現金" },
  );
  assert.deepEqual(out, {
    "セブン": { account_code: "5010", account_name: "食費" },
  });
});

test("normalizeSuggestions: account_map に無い code は skip", () => {
  const out = normalizeSuggestions(
    { results: [{ index: 0, account_code: "9999" }] },
    [{ description: "x" }],
    { "5010": "食費" },
  );
  assert.deepEqual(out, {});
});

test("normalizeSuggestions: results 非配列で空", () => {
  assert.deepEqual(normalizeSuggestions({}, [], {}), {});
  assert.deepEqual(normalizeSuggestions(null, [], {}), {});
});


// ============ runSuggestCategories 統合 ============

test("正常フロー: prompt-context + ai-config → decrypt → LLM → normalize", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    [/\/suggest-categories\/prompt-context\?/, () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("sk-test"),
  }));
  const callLLMTextImpl = async (args) => {
    llmArgs = args;
    return {
      result: { results: [{ index: 0, account_code: "5010" }] },
      usage: { input_tokens: 200, output_tokens: 50 },
    };
  };
  const ret = await runSuggestCategories({
    paymentAccountCode: "1010",
    rows: [{ description: "セブン", deposit: 0, withdrawal: 500 }],
    client, callLLMTextImpl, fetchImpl,
  });
  assert.deepEqual(ret, {
    "セブン": { account_code: "5010", account_name: "食費" },
  });
  // prompt が組み立てられている
  assert.match(llmArgs.prompt, /現金/);
  assert.match(llmArgs.prompt, /セブン/);
  assert.equal(llmArgs.apiKey, "sk-test");
  assert.equal(llmArgs.model, "gpt-4o");
  assert.equal(llmArgs.maxTokens, 4000);
});

test("model_name 空ならデフォルトモデル使用", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    [/\/suggest-categories\/prompt-context\?/, () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "anthropic", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await runSuggestCategories({
    paymentAccountCode: "1010",
    rows: [{ description: "x" }],
    client, fetchImpl,
    callLLMTextImpl: async (args) => {
      llmArgs = args;
      return { result: { results: [] }, usage: {} };
    },
  });
  assert.equal(llmArgs.model, "claude-sonnet-4-20250514");
});

test("非 E2EE config で throw", async () => {
  const fetchImpl = makeFetch([
    [/\/suggest-categories\/prompt-context\?/, () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", is_e2ee: false,
      api_key_blob: null, api_key_iv: null,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  await assert.rejects(
    () => runSuggestCategories({
      paymentAccountCode: "1010",
      rows: [{ description: "x" }],
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /E2EE 形式ではありません/,
  );
});

test("未対応 provider で throw", async () => {
  const fetchImpl = makeFetch([
    [/\/suggest-categories\/prompt-context\?/, () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "evil", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => runSuggestCategories({
      paymentAccountCode: "1010",
      rows: [{ description: "x" }],
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /unsupported provider/,
  );
});

test("prompt-context エラーで rejection", async () => {
  const fetchImpl = makeFetch([
    [/\/suggest-categories\/prompt-context\?/,
     () => jsonResp({ error: "no account" }, false, 400)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => runSuggestCategories({
      paymentAccountCode: "1010",
      rows: [{ description: "x" }],
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /prompt-context fetch failed/,
  );
});

test("必須引数欠如で throw", async () => {
  await assert.rejects(
    () => runSuggestCategories({
      rows: [{ description: "x" }],
      client: { decrypt: () => {} },
    }),
    /paymentAccountCode is required/,
  );
  await assert.rejects(
    () => runSuggestCategories({
      paymentAccountCode: "1010",
      client: { decrypt: () => {} },
    }),
    /rows is required/,
  );
  await assert.rejects(
    () => runSuggestCategories({
      paymentAccountCode: "1010",
      rows: [],
      client: { decrypt: () => {} },
    }),
    /rows is required/,
  );
  await assert.rejects(
    () => runSuggestCategories({
      paymentAccountCode: "1010",
      rows: [{ description: "x" }],
    }),
    /client.*is required/,
  );
});
