// E2 PR-C-5b: web_extract.js + text-only LLM handlers の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const WEB_EXTRACT_URL = new URL(
  "../../../app/static/js/crypto/llm/web_extract.js",
  import.meta.url,
);
const INDEX_URL = new URL(
  "../../../app/static/js/crypto/llm/index.js",
  import.meta.url,
);
const OPENAI_URL = new URL(
  "../../../app/static/js/crypto/llm/openai.js",
  import.meta.url,
);
const ANTHROPIC_URL = new URL(
  "../../../app/static/js/crypto/llm/anthropic.js",
  import.meta.url,
);
const GOOGLE_URL = new URL(
  "../../../app/static/js/crypto/llm/google.js",
  import.meta.url,
);

const {
  buildWebExtractPrompt, normalizeTransactions, runWebExtract,
} = await import(WEB_EXTRACT_URL.href);
const { callLLMText, LLM_TEXT_HANDLERS } = await import(INDEX_URL.href);
const { callOpenAIText } = await import(OPENAI_URL.href);
const { callAnthropicText } = await import(ANTHROPIC_URL.href);
const { callGoogleText } = await import(GOOGLE_URL.href);


function makeFetch(responder) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return responder(url, init);
  };
  fn.calls = calls;
  return fn;
}

function jsonResp(body, ok = true, status = 200) {
  return {
    ok, status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}


// ============ buildWebExtractPrompt ============

test("buildWebExtractPrompt: プレースホルダを置換", () => {
  const p = buildWebExtractPrompt({
    promptTemplate: "口座: __PAYMENT_ACCOUNT_NAME__\n本文: __RAW_TEXT__",
    paymentAccountName: "三井住友",
    rawText: "5/15 ATM 5000",
  });
  assert.equal(p, "口座: 三井住友\n本文: 5/15 ATM 5000");
});

test("buildWebExtractPrompt: 50000 字で切り詰め", () => {
  const long = "x".repeat(60000);
  const p = buildWebExtractPrompt({
    promptTemplate: "__RAW_TEXT__",
    paymentAccountName: "p",
    rawText: long,
  });
  assert.equal(p.length, 50000);
});

test("buildWebExtractPrompt: 必須引数欠如で throw", () => {
  assert.throws(
    () => buildWebExtractPrompt({ paymentAccountName: "x", rawText: "y" }),
    /promptTemplate is required/,
  );
  assert.throws(
    () => buildWebExtractPrompt({
      promptTemplate: "t", paymentAccountName: "", rawText: "y",
    }),
    /paymentAccountName is required/,
  );
  assert.throws(
    () => buildWebExtractPrompt({
      promptTemplate: "t", paymentAccountName: "x", rawText: 123,
    }),
    /rawText must be a string/,
  );
});


// ============ normalizeTransactions ============

test("normalizeTransactions: row_num 付与 + 整数化", () => {
  const txs = normalizeTransactions({
    transactions: [
      { date: "2026-02-15", description: "ATM",
        deposit: "0", withdrawal: "5000" },
      { date: "2026-02-16", description: "給与",
        deposit: 250000, withdrawal: 0 },
    ],
  });
  assert.equal(txs.length, 2);
  assert.equal(txs[0].row_num, 1);
  assert.equal(txs[0].deposit, 0);
  assert.equal(txs[0].withdrawal, 5000);
  assert.equal(txs[1].row_num, 2);
  assert.equal(txs[1].deposit, 250000);
});

test("normalizeTransactions: 不正値はゼロ", () => {
  const txs = normalizeTransactions({
    transactions: [
      { date: null, description: null,
        deposit: "abc", withdrawal: undefined },
    ],
  });
  assert.equal(txs[0].date, null);
  assert.equal(txs[0].description, "");
  assert.equal(txs[0].deposit, 0);
  assert.equal(txs[0].withdrawal, 0);
});

test("normalizeTransactions: transactions 非配列 → 空配列", () => {
  assert.deepEqual(normalizeTransactions(null), []);
  assert.deepEqual(normalizeTransactions({}), []);
  assert.deepEqual(normalizeTransactions({ transactions: "not-array" }), []);
});


// ============ callLLMText (provider dispatch) ============

test("callLLMText: 不正 provider で throw", async () => {
  await assert.rejects(
    () => callLLMText({ provider: "evil", apiKey: "k", model: "m",
                        prompt: "p", fetchImpl: makeFetch(() => jsonResp({})) }),
    /unknown provider/,
  );
});

test("LLM_TEXT_HANDLERS: 3 provider 揃っている", () => {
  assert.equal(typeof LLM_TEXT_HANDLERS.openai, "function");
  assert.equal(typeof LLM_TEXT_HANDLERS.anthropic, "function");
  assert.equal(typeof LLM_TEXT_HANDLERS.google, "function");
});


// ============ 各 text handler の fetch リクエスト検証 ============

test("callOpenAIText: chat completions 互換 body + Bearer", async () => {
  const fetchImpl = makeFetch(() => jsonResp({
    choices: [{ message: { content: '{"transactions":[]}' } }],
    usage: { prompt_tokens: 10, completion_tokens: 5 },
  }));
  const res = await callOpenAIText({
    apiKey: "sk-test", model: "gpt-4o-mini", prompt: "P",
    fetchImpl,
  });
  assert.deepEqual(res.result, { transactions: [] });
  assert.equal(res.usage.input_tokens, 10);
  assert.equal(res.usage.output_tokens, 5);
  const c = fetchImpl.calls[0];
  assert.match(c.url, /chat\/completions$/);
  assert.equal(c.init.headers.Authorization, "Bearer sk-test");
  const body = JSON.parse(c.init.body);
  assert.equal(body.model, "gpt-4o-mini");
  assert.equal(body.messages[0].role, "user");
  assert.equal(body.messages[0].content, "P");
});

test("callAnthropicText: x-api-key + content text のみ", async () => {
  const fetchImpl = makeFetch(() => jsonResp({
    content: [{ text: '{"transactions":[]}' }],
    usage: { input_tokens: 20, output_tokens: 8 },
  }));
  const res = await callAnthropicText({
    apiKey: "sk-ant", model: "claude-sonnet-4", prompt: "P", fetchImpl,
  });
  assert.deepEqual(res.result, { transactions: [] });
  assert.equal(res.usage.input_tokens, 20);
  const c = fetchImpl.calls[0];
  assert.equal(c.init.headers["x-api-key"], "sk-ant");
  assert.equal(c.init.headers["anthropic-version"], "2023-06-01");
  const body = JSON.parse(c.init.body);
  assert.equal(body.messages[0].content[0].type, "text");
  assert.equal(body.messages[0].content[0].text, "P");
});

test("callGoogleText: ?key= query + parts.text のみ + responseMimeType=json", async () => {
  const fetchImpl = makeFetch(() => jsonResp({
    candidates: [{ content: { parts: [{ text: '{"transactions":[]}' }] } }],
    usageMetadata: { promptTokenCount: 30, candidatesTokenCount: 12 },
  }));
  const res = await callGoogleText({
    apiKey: "g-key", model: "gemini-2.0-flash", prompt: "P", fetchImpl,
  });
  assert.deepEqual(res.result, { transactions: [] });
  assert.equal(res.usage.output_tokens, 12);
  const c = fetchImpl.calls[0];
  assert.match(c.url, /:generateContent\?key=g-key$/);
  const body = JSON.parse(c.init.body);
  assert.equal(body.contents[0].parts[0].text, "P");
  assert.equal(body.generationConfig.responseMimeType, "application/json");
});

test("callOpenAIText: HTTP エラーで throw", async () => {
  const fetchImpl = makeFetch(() => jsonResp("oops", false, 500));
  await assert.rejects(
    () => callOpenAIText({
      apiKey: "k", model: "m", prompt: "P", fetchImpl,
    }),
    /OpenAI API error: HTTP 500/,
  );
});

test("callAnthropicText: HTTP エラーで throw", async () => {
  const fetchImpl = makeFetch(() => jsonResp("oops", false, 502));
  await assert.rejects(
    () => callAnthropicText({
      apiKey: "k", model: "m", prompt: "P", fetchImpl,
    }),
    /Anthropic API error: HTTP 502/,
  );
});

test("callGoogleText: HTTP エラーで throw", async () => {
  const fetchImpl = makeFetch(() => jsonResp("oops", false, 503));
  await assert.rejects(
    () => callGoogleText({
      apiKey: "k", model: "m", prompt: "P", fetchImpl,
    }),
    /Google API error: HTTP 503/,
  );
});

test("callOpenAIText: 必須引数欠如で throw", async () => {
  await assert.rejects(
    () => callOpenAIText({ model: "m", prompt: "P" }),
    /apiKey is required/,
  );
  await assert.rejects(
    () => callOpenAIText({ apiKey: "k", prompt: "P" }),
    /model is required/,
  );
  await assert.rejects(
    () => callOpenAIText({ apiKey: "k", model: "m" }),
    /prompt is required/,
  );
});

test("callAnthropicText / callGoogleText: 必須引数欠如で throw", async () => {
  await assert.rejects(
    () => callAnthropicText({ model: "m", prompt: "P" }),
    /apiKey is required/,
  );
  await assert.rejects(
    () => callGoogleText({ model: "m", prompt: "P" }),
    /apiKey is required/,
  );
});


// ============ runWebExtract 統合 ============

test("runWebExtract: 全体フロー (prompt 構築 → LLM → normalize)", async () => {
  let receivedArgs;
  const callLLMTextImpl = async (args) => {
    receivedArgs = args;
    return {
      result: {
        transactions: [
          { date: "2026-02-15", description: "ATM",
            deposit: 0, withdrawal: 5000 },
        ],
      },
      usage: { input_tokens: 100, output_tokens: 30 },
    };
  };
  const res = await runWebExtract({
    promptContext: {
      prompt_template: "口座: __PAYMENT_ACCOUNT_NAME__\n本文: __RAW_TEXT__",
    },
    provider: "openai", apiKey: "sk", model: "gpt-4o",
    paymentAccountName: "三井住友", rawText: "5/15 ATM 5000",
    callLLMTextImpl,
  });
  // prompt が構築されている
  assert.equal(
    receivedArgs.prompt,
    "口座: 三井住友\n本文: 5/15 ATM 5000",
  );
  assert.equal(receivedArgs.provider, "openai");
  assert.equal(receivedArgs.apiKey, "sk");
  assert.equal(receivedArgs.maxTokens, 16000);
  // 戻り値が整形されている
  assert.equal(res.transactions.length, 1);
  assert.equal(res.transactions[0].row_num, 1);
  assert.equal(res.transactions[0].withdrawal, 5000);
  assert.equal(res.usage.input_tokens, 100);
});

test("runWebExtract: promptContext 欠如で throw", async () => {
  await assert.rejects(
    () => runWebExtract({
      provider: "openai", apiKey: "k", model: "m",
      paymentAccountName: "x", rawText: "y",
      callLLMTextImpl: async () => ({}),
    }),
    /promptContext is required/,
  );
});

test("runWebExtract: promptContext.prompt_template 欠如で早期 throw", async () => {
  // PR #164 review Minor 4: promptContext オブジェクトだが prompt_template が
  // 欠落しているケースを runWebExtract 側で早めに弾く
  await assert.rejects(
    () => runWebExtract({
      promptContext: { custom_prompt: "x" },
      provider: "openai", apiKey: "k", model: "m",
      paymentAccountName: "x", rawText: "y",
      callLLMTextImpl: async () => ({}),
    }),
    /prompt_template is required/,
  );
});

test("runWebExtract: LLM エラーは伝播", async () => {
  await assert.rejects(
    () => runWebExtract({
      promptContext: { prompt_template: "__RAW_TEXT__" },
      provider: "openai", apiKey: "k", model: "m",
      paymentAccountName: "x", rawText: "y",
      callLLMTextImpl: async () => { throw new Error("LLM crash"); },
    }),
    /LLM crash/,
  );
});
