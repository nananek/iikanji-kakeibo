"""CSV明細照合（マッチング）の AI プロンプト材料。

E3-F PR-D-2: 決定論的マッチング (旧 find_matches / _build_daily_summary) は
平文 date / description / source を読むため、クライアント側
`crypto/reconcile/classical.js` に移植して削除した。サーバに残るのは AI 照合
用のプロンプトテンプレートとバッチサイズのみ (LLM 呼出も
`reconcile_orchestrator.js` がクライアント完結で行う)。
"""

# --- AI 照合 ---

# 旧 AI_RECONCILE_PROMPT (Python str.format 版) は find_ai_matches 廃止に
# 伴い削除済。クライアント側 reconcile_orchestrator.js が
# AI_RECONCILE_PROMPT_TEMPLATE (placeholder 版) を使用する。
AI_RECONCILE_PROMPT_TEMPLATE = """\
あなたは日本の家計簿アプリの照合アシスタントです。
以下はクレジットカード等のCSV明細と、既存の仕訳一覧です。
金額が完全一致しないものの、同一取引である可能性があるペアを見つけてください。

照合のヒント:
- 摘要テキストの類似性（例: CSVの「アマゾン」と仕訳の「Amazon.co.jp」）
- 日付の近さ（クレジットカードは利用日と計上日にずれが生じやすい）
- 端数の違い（ポイント利用・割引で金額が僅かに異なるケース）
- 分割払いの合計と一括の対応

## CSV明細（未照合）
__CSV_ROWS_TEXT__

## 既存仕訳（未照合）
__JOURNAL_ROWS_TEXT__

各CSV行に対して、最も可能性の高い仕訳候補を1件（確信度とともに）提案してください。
確信度が低い場合（0.3未満）は候補なしとしてください。

必ず以下のJSON形式のみを返してください。他のテキストは含めないでください。
{"matches": [
  {"csv_index": 0, "entry_id": 123, "confidence": 0.85, "reason": "摘要が類似"},
  {"csv_index": 1, "entry_id": null, "confidence": 0, "reason": "該当なし"}
]}"""

AI_RECONCILE_BATCH_SIZE = 30


# find_ai_matches / find_matches / _build_daily_summary は E2EE 化に伴い削除。
# 決定論的マッチングはクライアント側 crypto/reconcile/classical.js が、
# AI 照合は reconcile_orchestrator.js が等価の処理を実行する。
