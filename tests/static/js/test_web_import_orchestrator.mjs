// E2 PR-C-5c: web_import_orchestrator.extractAndSaveWebText の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ORCH = new URL(
  "../../../app/static/js/crypto/web_import_orchestrator.js",
  import.meta.url,
);
const { extractAndSaveWebText } = await import(ORCH.href);


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
    "口座: __PAYMENT_ACCOUNT_NAME__\n本文:\n__RAW_TEXT__",
  custom_prompt: "",
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


test("正常フロー: prompt-context + ai-config → decrypt → extract → POST", async () => {
  let savedPayload = null;
  const fetchImpl = makeFetch([
    ["/api/v1/web-import/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o-mini",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/web-import/", (init) => {
      savedPayload = JSON.parse(init.body);
      return jsonResp({ ok: true, redirect_url: "/web-import/confirm" });
    }],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("sk-test"),
  }));
  let extractArgs;
  const runWebExtractImpl = async (args) => {
    extractArgs = args;
    return {
      transactions: [
        { row_num: 1, date: "2026-02-15", description: "ATM",
          deposit: 0, withdrawal: 5000 },
      ],
      usage: { input_tokens: 100, output_tokens: 30 },
      raw: {},
    };
  };

  const ret = await extractAndSaveWebText({
    rawText: "5/15 ATM 5000",
    paymentAccountCode: "1010",
    paymentAccountName: "三井住友",
    client, runWebExtractImpl, fetchImpl,
  });

  assert.equal(ret.redirect_url, "/web-import/confirm");
  assert.equal(ret.transactions.length, 1);
  assert.equal(ret.usage.input_tokens, 100);

  // runWebExtract 引数検証
  assert.equal(extractArgs.provider, "openai");
  assert.equal(extractArgs.apiKey, "sk-test");
  assert.equal(extractArgs.model, "gpt-4o-mini");
  assert.equal(extractArgs.paymentAccountName, "三井住友");
  assert.equal(extractArgs.rawText, "5/15 ATM 5000");

  // POST body 検証
  assert.equal(savedPayload.parsed_transactions.length, 1);
  assert.equal(savedPayload.payment_account_code, "1010");
});


test("model_name 空ならデフォルトモデル使用", async () => {
  let extractArgs;
  const fetchImpl = makeFetch([
    ["/api/v1/web-import/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "anthropic", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/web-import/", () => jsonResp({ ok: true, redirect_url: "/x" })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await extractAndSaveWebText({
    rawText: "y", paymentAccountCode: "1010", paymentAccountName: "p",
    client, fetchImpl,
    runWebExtractImpl: async (args) => {
      extractArgs = args;
      return { transactions: [], usage: {}, raw: {} };
    },
  });
  assert.equal(extractArgs.model, "claude-sonnet-4-20250514");
});


test("default_model_by_provider に provider 未定義なら throw", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/web-import/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "evil", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountCode: "1010", paymentAccountName: "p",
      client, fetchImpl,
      runWebExtractImpl: async () => ({}),
    }),
    /unsupported provider/,
  );
});


test("AI config が E2EE 形式でなければ throw", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/web-import/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", is_e2ee: false,
      api_key_blob: null, api_key_iv: null,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountCode: "1010", paymentAccountName: "p",
      client, fetchImpl,
      runWebExtractImpl: async () => ({}),
    }),
    /E2EE 形式ではありません/,
  );
});


test("prompt-context エラーで rejection", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/web-import/prompt-context",
     () => jsonResp({ error: "boom" }, false, 500)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountCode: "1010", paymentAccountName: "p",
      client, fetchImpl,
      runWebExtractImpl: async () => ({}),
    }),
    /prompt-context fetch failed/,
  );
});


test("save エラーで rejection (extract は完了済)", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/web-import/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/web-import/",
     () => jsonResp({ error: "session full" }, false, 500)],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountCode: "1010", paymentAccountName: "p",
      client, fetchImpl,
      runWebExtractImpl: async () => ({
        transactions: [{ date: "x" }], usage: {}, raw: {},
      }),
    }),
    /save parsed failed/,
  );
});


test("必須引数欠如で throw", async () => {
  await assert.rejects(
    () => extractAndSaveWebText({
      paymentAccountCode: "1010", paymentAccountName: "p",
      client: { decrypt: () => {} },
    }),
    /rawText is required/,
  );
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountName: "p",
      client: { decrypt: () => {} },
    }),
    /paymentAccountCode is required/,
  );
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountCode: "1010",
      client: { decrypt: () => {} },
    }),
    /paymentAccountName is required/,
  );
  await assert.rejects(
    () => extractAndSaveWebText({
      rawText: "y", paymentAccountCode: "1010", paymentAccountName: "p",
    }),
    /client.*is required/,
  );
});
