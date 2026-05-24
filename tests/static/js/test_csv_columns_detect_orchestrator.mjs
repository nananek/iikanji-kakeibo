// csv_columns_detect_orchestrator の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ORCH = new URL(
  "../../../app/static/js/crypto/csv_columns_detect_orchestrator.js",
  import.meta.url,
);
const {
  buildDetectPrompt, validateMapping, runColumnsDetect,
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
    "H:__HEADERS_TEXT__\nC:__SAMPLE_COUNT__\nS:__SAMPLE_TEXT__",
  headers_text: "[0] 日付, [1] 摘要, [2] 入金, [3] 出金",
  sample_text: "2026/01/01, テスト, 1000, ",
  sample_count: 1,
  num_cols: 4,
  custom_prompt: "",
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


// ============ buildDetectPrompt ============

test("buildDetectPrompt: 3 プレースホルダ置換", () => {
  const p = buildDetectPrompt({
    promptTemplate: "[H:__HEADERS_TEXT__][C:__SAMPLE_COUNT__][S:__SAMPLE_TEXT__]",
    headersText: "H1",
    sampleText: "S1",
    sampleCount: 2,
  });
  assert.equal(p, "[H:H1][C:2][S:S1]");
});

test("buildDetectPrompt: promptTemplate 必須", () => {
  assert.throws(
    () => buildDetectPrompt({}),
    /promptTemplate is required/,
  );
});

test("buildDetectPrompt: 値中の __XXX__ は再展開されない (二重展開防止)", () => {
  // headersText に SAMPLE_TEXT プレースホルダ文字列が混入しても
  // 後段の置換で展開されてはならない (プロンプトインジェクション対策)
  const p = buildDetectPrompt({
    promptTemplate: "[H:__HEADERS_TEXT__][S:__SAMPLE_TEXT__]",
    headersText: "__SAMPLE_TEXT__",
    sampleText: "REAL",
    sampleCount: 1,
  });
  assert.equal(p, "[H:__SAMPLE_TEXT__][S:REAL]");
});

test("buildDetectPrompt: customPrompt を末尾に追加", () => {
  const p = buildDetectPrompt({
    promptTemplate: "BASE",
    headersText: "", sampleText: "", sampleCount: 0,
    customPrompt: "○○銀行はマイナスがキャッシュバック",
  });
  assert.equal(p, "BASE\n\n## ユーザー定型情報\n○○銀行はマイナスがキャッシュバック");
});

test("buildDetectPrompt: customPrompt 空文字なら追加しない", () => {
  const p = buildDetectPrompt({
    promptTemplate: "BASE",
    headersText: "", sampleText: "", sampleCount: 0,
    customPrompt: "",
  });
  assert.equal(p, "BASE");
});



// ============ validateMapping ============

test("validateMapping: 正常マッピング", () => {
  const m = validateMapping({
    date_col: 0, desc_col: 1,
    deposit_col: 2, withdrawal_col: 3,
    date_format: "%Y/%m/%d",
  }, 4);
  assert.deepEqual(m, {
    date_col: 0, desc_col: 1,
    deposit_col: 2, withdrawal_col: 3,
    date_format: "%Y/%m/%d",
  });
});

test("validateMapping: withdrawal_only", () => {
  const m = validateMapping({
    date_col: 0, desc_col: 1,
    deposit_col: null, withdrawal_col: 2,
    date_format: "%Y/%m/%d",
  }, 3);
  assert.equal(m.deposit_col, null);
  assert.equal(m.withdrawal_col, 2);
});

test("validateMapping: 範囲外 → null", () => {
  assert.equal(validateMapping({
    date_col: 99, desc_col: 1, date_format: "%Y/%m/%d",
  }, 4), null);
});

test("validateMapping: 必須欠如 → null", () => {
  assert.equal(validateMapping({date_col: 0}, 4), null);
});

test("validateMapping: 非 dict → null", () => {
  assert.equal(validateMapping(null, 4), null);
  assert.equal(validateMapping("x", 4), null);
});

test("validateMapping: date_format デフォルト", () => {
  const m = validateMapping({date_col: 0, desc_col: 1}, 2);
  assert.equal(m.date_format, "%Y/%m/%d");
});


// ============ runColumnsDetect 統合 ============

test("正常フロー: prompt-context POST → ai-config → decrypt → LLM → validate", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    ["/csv-import/api/columns-detect-context", () => jsonResp(PROMPT_CTX)],
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
      result: {
        date_col: 0, desc_col: 1,
        deposit_col: 2, withdrawal_col: 3,
        date_format: "%Y/%m/%d",
      },
      usage: {},
    };
  };
  const ret = await runColumnsDetect({
    headers: ["日付", "摘要", "入金", "出金"],
    sampleRows: [["2026/01/01", "テスト", "1000", ""]],
    client, callLLMTextImpl, fetchImpl,
  });
  assert.deepEqual(ret, {
    date_col: 0, desc_col: 1,
    deposit_col: 2, withdrawal_col: 3,
    date_format: "%Y/%m/%d",
  });
  assert.equal(llmArgs.apiKey, "sk-test");
  assert.equal(llmArgs.model, "gpt-4o");
  assert.equal(llmArgs.maxTokens, 500);
});

test("非 E2EE config で throw", async () => {
  const fetchImpl = makeFetch([
    ["/csv-import/api/columns-detect-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", is_e2ee: false,
      api_key_blob: null, api_key_iv: null,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  await assert.rejects(
    () => runColumnsDetect({
      headers: ["a"], sampleRows: [],
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /E2EE 形式ではありません/,
  );
});

test("未対応 provider (default_model_by_provider に無い) で throw", async () => {
  const fetchImpl = makeFetch([
    ["/csv-import/api/columns-detect-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "evil", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => runColumnsDetect({
      headers: ["a"], sampleRows: [],
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /unsupported provider/,
  );
});

test("model_name 空 + default も空ならモデル名未設定エラー", async () => {
  // 対応プロバイダだが default_model_by_provider[provider] が空文字
  const ctx = {
    ...PROMPT_CTX,
    default_model_by_provider: { openai: "" },
  };
  const fetchImpl = makeFetch([
    ["/csv-import/api/columns-detect-context", () => jsonResp(ctx)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => runColumnsDetect({
      headers: ["a"], sampleRows: [],
      client, fetchImpl,
      callLLMTextImpl: async () => ({}),
    }),
    /モデル名が指定されていません/,
  );
});

test("model_name 空ならデフォルトモデル使用", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    ["/csv-import/api/columns-detect-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "anthropic", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await runColumnsDetect({
    headers: ["a"], sampleRows: [],
    client, fetchImpl,
    callLLMTextImpl: async (args) => {
      llmArgs = args;
      return {
        result: { date_col: 0, desc_col: 0, date_format: "%Y" },
        usage: {},
      };
    },
  });
  assert.equal(llmArgs.model, "claude-sonnet-4-20250514");
});

test("validation 失敗で null 返却 (range out)", async () => {
  const fetchImpl = makeFetch([
    ["/csv-import/api/columns-detect-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  const ret = await runColumnsDetect({
    headers: ["a", "b"], sampleRows: [],
    client, fetchImpl,
    callLLMTextImpl: async () => ({
      result: { date_col: 99, desc_col: 1, date_format: "%Y" },
      usage: {},
    }),
  });
  // num_cols=4 (PROMPT_CTX) なので 99 は範囲外 → null
  // 注意: validateMapping は PROMPT_CTX.num_cols=4 を使うので 99>=4 で null
  assert.equal(ret, null);
});

test("headers 空で throw", async () => {
  await assert.rejects(
    () => runColumnsDetect({
      headers: [], sampleRows: [],
      client: { decrypt: () => {} },
    }),
    /headers is required/,
  );
});

test("client 欠如で throw", async () => {
  await assert.rejects(
    () => runColumnsDetect({
      headers: ["a"], sampleRows: [],
    }),
    /client.*is required/,
  );
});
