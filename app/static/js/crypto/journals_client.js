// Phase E3-C: クライアント側 仕訳 (JournalEntry) 取得 + 復号 helper。
//
// `GET /api/v1/journals?fiscal_year=YYYY` をページネーション全件取得し、
// レスポンスの encrypted_blob / blob_iv を MK で復号して plain object 配列を
// 返す。dual read 期間中は blob/iv が null の (未移行) 行もサポートし、
// 旧平文フィールド (date / description / lines[].account_code 等) で
// 同じ shape の object を組み立てる。
//
// これがクライアント側レポート (試算表 / P/L / B/S / etc, E3-C-2〜) の
// データソース基盤。設計書 §12.3 / §12.9 参照。

import { b64decode } from "./b64.js";
import { buildAAD, decryptRecord } from "./record.js";


/**
 * API entry の encrypted_blob を復号し、entry レベルの非 line フィールド
 * (date / description / source / batch_id / fiscal_period) を取り出す。
 *
 * 復号できない場合 (AAD 不一致 / bit flip / 鍵不一致 / blob 欠落) は closing
 * (損益振替・自動生成) 仕訳なら保持列から合成し、それ以外は null/空を返す
 * (E3-F PR-D-6-3b で平文フォールバックは廃止)。closing 仕訳はサーバが MK を
 * 持たず暗号化できないため encrypted_blob が空 (body=null) で、is_closing /
 * fiscal_year の保持列から date=年末 12/31・description="損益振替仕訳（自動
 * 生成）"・source="closing"・fiscal_period=16 を合成する (サーバ
 * fiscal.generate_closing_entries と一致)。
 *
 * @returns {Promise<{date, description, source, batch_id, fiscal_period}>}
 */
export async function decryptEntryMeta(client, userId, apiEntry) {
  let body = null;
  if (apiEntry.encrypted_blob && apiEntry.blob_iv) {
    try {
      const blob = b64decode(apiEntry.encrypted_blob);
      const iv = b64decode(apiEntry.blob_iv);
      const aad = buildAAD("je", userId);
      body = await decryptRecord(client, blob, iv, aad);
    } catch (e) {
      // 1 件の異常で全件取得が失敗しないよう entry 単位で局所化する。
      console.warn(
        `journals_client: entry ${apiEntry.id} decrypt failed: ` +
        `${e?.message || e}`,
      );
    }
  }
  const isClosing = apiEntry.is_closing ?? false;
  return {
    date: body?.date ?? (isClosing ? `${apiEntry.fiscal_year}-12-31` : null),
    description: body?.description ?? (isClosing ? "損益振替仕訳（自動生成）" : ""),
    source: body?.source ?? (isClosing ? "closing" : ""),
    // batch_id は entryBody に含まれない (batch top-level に集約) ため body には
    // 無い。平文カラムは保持されるが API レスポンスには含めない方針。
    batch_id: body?.batch_id ?? null,
    fiscal_period: body?.fiscal_period ?? (isClosing ? 16 : null),
  };
}


/**
 * 1 つの API entry を復号して正規化形式に変換。
 *
 * entry レベルは decryptEntryMeta、line は _normalizeLine で復号する。
 *
 * @returns {Promise<Object>} {id, fiscal_year, date, description, source, lines: [{account_code, debit, credit, description}]}
 */
async function _normalizeEntry(client, userId, apiEntry) {
  const meta = await decryptEntryMeta(client, userId, apiEntry);
  // _normalizeLine 内の throw (apiLine.id 欠落等) が Promise.all を突き抜け
  // て fetchJournalsForYear 全体を reject させないよう、line 単位で catch して
  // 平文フォールバックに局所化する (entry の try/catch と同じ方針)。
  const lines = await Promise.all(
    (apiEntry.lines || []).map((line) =>
      _normalizeLine(client, userId, apiEntry.id, line).catch((e) => {
        console.warn(
          `journals_client: line normalization failed ` +
          `(entry=${apiEntry.id}): ${e?.message || e}`,
        );
        return {
          account_code: line.account_code ?? null,
          debit: line.debit ?? 0,
          credit: line.credit ?? 0,
          description: line.description ?? "",
        };
      }),
    ),
  );
  return {
    id: apiEntry.id,
    fiscal_year: apiEntry.fiscal_year,
    // entry_number は非機密の連番 (平文カラム・DROP 対象外)。一覧 (出納帳/
    // 仕訳帳) の "No." 列表示に使うため API レスポンスからそのまま伝播する。
    entry_number: apiEntry.entry_number ?? null,
    // E3-F PR-D-6-3b: 平文 date/description/source/fiscal_period は API から
    // 撤去済 (date 列等は D-6-5 で DROP)。decryptEntryMeta が通常仕訳は復号
    // blob から、closing 仕訳は保持列から合成する (復号失敗 + 非 closing は
    // null/空。dual-read 平文フォールバックは廃止)。
    date: meta.date,
    description: meta.description,
    source: meta.source,
    batch_id: meta.batch_id,
    fiscal_period: meta.fiscal_period,
    // 以下は非暗号化メタ (平文カラム / DROP 対象外)。一覧 (仕訳帳) の編集可否
    // 判定・ソース/証憑バッジ描画・レポートの期間/closing 判定に使う:
    //   is_closing  : 損益振替 (自動生成) 判定。変更不可
    //   fiscal_month: 確定済み期間判定 (period <= closed_period) + 集計の期間判定
    //   vouchers    : 証憑画像リンク ([{id, uploaded_at}])
    is_closing: apiEntry.is_closing ?? false,
    fiscal_month: apiEntry.fiscal_month ?? null,
    vouchers: apiEntry.vouchers ?? [],
    lines,
  };
}


async function _normalizeLine(client, userId, entryId, apiLine) {
  let body = null;
  if (apiLine.encrypted_blob && apiLine.blob_iv) {
    try {
      const blob = b64decode(apiLine.encrypted_blob);
      const iv = b64decode(apiLine.blob_iv);
      // E3-F PR-A: AAD は Option B (user_id のみ)。entry_id / line_id swap
      // 検知能力は失う代わりに新規 POST 時の AAD 構築が単純化される。
      const aad = buildAAD("jel", userId);
      body = await decryptRecord(client, blob, iv, aad);
    } catch (e) {
      console.warn(
        `journals_client: line ${apiLine.id} (entry=${entryId}) decrypt ` +
        `failed, falling back to plaintext: ${e?.message || e}`,
      );
    }
  }
  return {
    account_code: body?.account_code ?? apiLine.account_code,
    debit: body?.debit_amount ?? apiLine.debit ?? 0,
    credit: body?.credit_amount ?? apiLine.credit ?? 0,
    description: body?.description ?? apiLine.description ?? "",
  };
}


/**
 * 指定年度の全仕訳を取得 + 復号。
 *
 * ページネーションを自動で回し全件取得する。100 件/ページで取得し、
 * total >= 累計取得数に達するまで繰り返す。
 *
 * @param {Object} args
 * @param {Object} args.client                       SharedCryptoClient (decrypt 用)
 * @param {number|bigint} args.userId                 復号 AAD に使う
 * @param {number} args.fiscalYear
 * @param {Function} [args.fetchImpl]                 テスト DI
 * @param {number} [args.perPage=100]
 * @returns {Promise<Array<Object>>}                  正規化された entry 配列
 */
export async function fetchJournalsForYear({
  client, userId, fiscalYear, fetchImpl, perPage = 100,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (userId === undefined || userId === null) {
    throw new Error("userId is required");
  }
  if (typeof userId !== "number" && typeof userId !== "bigint") {
    throw new Error("userId must be a number or bigint");
  }
  if (typeof userId === "number" && !Number.isSafeInteger(userId)) {
    throw new Error("userId Number must be a safe integer (use BigInt for > 2^53)");
  }
  if (!Number.isInteger(fiscalYear) || !(1900 <= fiscalYear && fiscalYear <= 2200)) {
    throw new Error("fiscalYear must be int in 1900..2200");
  }
  const f = fetchImpl ?? globalThis.fetch;

  const all = [];
  let page = 1;
  while (true) {
    const url = `/api/v1/journals?fiscal_year=${fiscalYear}&page=${page}&per_page=${perPage}`;
    const r = await f(url, { credentials: "include" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(`fetchJournalsForYear: HTTP ${r.status} ${e.error || ""}`);
    }
    const body = await r.json();
    const journals = body.journals || [];
    for (const apiEntry of journals) {
      all.push(await _normalizeEntry(client, userId, apiEntry));
    }
    // 打ち切り条件:
    //   1. 当該ページが空 → これ以上ない
    //   2. total が定義されており、累計取得数が total に達した
    // `body.total || 0` だと total=0 + journals 非空のサーババグ時に
    // 即 break して 2 ページ目以降を取得しないので、明示的に分離する。
    if (journals.length === 0) break;
    if (typeof body.total === "number" && all.length >= body.total) break;
    page += 1;
    if (page > 1000) {
      throw new Error("fetchJournalsForYear: pagination exceeded 1000 pages");
    }
  }
  return all;
}


/**
 * 単一仕訳の entry レベルフィールド (date / description / source / fiscal_period
 * / batch_id) を `GET /api/v1/journals/<id>` から取得 + 復号して返す。
 *
 * 編集フォーム (仕訳帳 / 出納帳) の date/description prefill 用。サーバは平文
 * date/description を返さなくなった (E3-F PR-D-6-3b-3) ため、クライアントが
 * 自分の MK で復号して値を埋める。/api/v1/journals/<id> は本人 (g.auth_user)
 * の仕訳のみを返す = 編集は本人操作前提なので適合する (監査代理は編集 submit
 * 自体がブロックされる)。
 *
 * @param {Object} args
 * @param {Object} args.client          SharedCryptoClient (decrypt 用)
 * @param {number|bigint} args.userId   復号 AAD に使う
 * @param {number} args.entryId
 * @param {Function} [args.fetchImpl]   テスト DI
 * @returns {Promise<{date, description, source, batch_id, fiscal_period}>}
 */
export async function fetchEntryFields({ client, userId, entryId, fetchImpl }) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (userId === undefined || userId === null) {
    throw new Error("userId is required");
  }
  if (entryId === undefined || entryId === null) {
    throw new Error("entryId is required");
  }
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f(`/api/v1/journals/${entryId}`, { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`fetchEntryFields: HTTP ${r.status} ${e.error || ""}`);
  }
  const body = await r.json();
  const apiEntry = body.journal;
  if (!apiEntry) {
    throw new Error("fetchEntryFields: response missing journal");
  }
  return decryptEntryMeta(client, userId, apiEntry);
}
