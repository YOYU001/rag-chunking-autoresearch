# CLAUDE.md

本檔案為 Claude Code（claude.ai/code）在此 repository 中工作時的指引。

## 這個 repository 是什麼

這是一個 **RAG chunking AutoResearch** 專案：讓自主 AI agent（Hermes）在固定的評分 harness 下反覆改寫 chunking 策略、自動實驗，找出更快且不犧牲內容品質的切法。主要程式在 `tools/chunking_autoresearch/`。

實驗迴圈目前已暫停（原因與成果見 `docs/chunking_autoresearch_findings_for_mvp1_2026-09-02.md`）。沒有 package manifest 或統一的 test runner；使用前請先確認。

## 與 `AI Energy Operations Copilot`（MVP_V1）的關係

這是一個**獨立的姊妹專案**，不是 MVP_V1 底下的子資料夾：

- MVP_V1 是**被評估對象（system under test）**——它的 RAG pipeline、`/assistant` chat endpoint、tool orchestration 都是這個平台要評測的目標。
- `datasets/*.json` 是從 MVP_V1 的 `spike/` 資料夾**複製**（不是搬移）過來的一次性起始快照。MVP_V1 自己的測試／腳本仍依賴原始檔案；這裡的副本之後會獨立演進，**不會**跟 MVP_V1 保持同步。不要把兩邊的檔案互相「修正」拉齊。

## 資料夾結構

```text
.claude/
  settings.json                                # 權限與 sandbox 設定（deny 讀取 .env 系列、安裝套件要確認等）
  agents/                                       # 自訂 subagent：QA.md、RESEARCH.md、CODE_REVIEW.md
  skills/                                       # 自訂 skill，格式為 <name>/SKILL.md + scripts/（periodic-housekeeping、api-cost-estimate）
  rules/                                        # 路徑限定規則（paths frontmatter），例如 python.md、sql.md
docs/
  chunking_autoresearch_findings_for_mvp1_2026-09-02.md  # 暫停前的實驗成果報告
  rag_chunking_evaluation_metrics_research.md         # chunking／RAG 評測指標研究
  spike_documents/                                    # harness 使用的 4 份測試 PDF
tools/chunking_autoresearch/                          # AutoResearch 主程式（harness、strategy、dashboard 等）
datasets/
  energyops_test_questions.json              # 29 題固定評測問題，複製自 MVP_V1 spike/test_questions.json
  energyops_retrieval_benchmark_baseline.json # MVP_V1 的 retrieval baseline 數字，作為 Baseline 參考
  energyops_chunking_comparison_baseline.json # Phase 5/6 之前用不到
results/                                      # 每次評測跑出來的結果放這裡（目前是空的）
active/                                        # 工作中的暫存區，不進版控（見下方說明）
  research/                                    # 探索性研究、筆記草稿
  execution/                                   # 執行中、尚未定案的評測中間產出
  config/                                      # 實驗用的臨時設定草稿
  temp/                                        # 純暫存，隨時可丟
```

`active/` 底下四個子資料夾各自只保留一個 `.gitkeep` 佔位，實際內容透過 `.gitignore` 排除在版控之外，可以放心在裡面產生、堆放任何還沒定案的東西。**定案的東西要手動搬到 `results/`（評測結果）、`docs/`（正式文件）、或 `datasets/`（定案的新資料集）**，`active/` 本身永遠不會出現在 commit 歷史裡。

## 這個 repo 的工作慣例

- **每次開始工作前，先閱讀 `PROGRESS.md` 與 `TODO.md`**：`PROGRESS.md` 了解目前進度與上次停在哪裡，工作結束或有階段性產出時新增一筆紀錄（日期 + 做了什麼 + 下一步）；`TODO.md` 是跨對話留存的待辦清單（打勾框樣式），完成項目要打勾並保留刪除線，不要直接刪除整行，新的待辦事項也隨時加進對應區塊。
- **`datasets/energyops_test_questions.json` 是 test oracle**（見其 `_meta.purpose`）：一旦 retrieval/citation 測試已經在使用它，就不要隨意修改題目或預期答案——修改它要當成變更 ground truth，而不是一般的資料修正。注意其 `verification_tiers` 欄位：只有 `verified` 的題目可以拿來算正式的 accuracy metric；`partially_verified` 僅能用於定性觀察；`unverified` 是行為面的判斷（系統是否恰當地拒答），不是事實比對。
- 執行付費 API 比較（含 LLM-as-a-Judge 呼叫）之前，先用本專案的 `api-cost-estimate` skill（`.claude/skills/api-cost-estimate/`）估算花費，不要盲跑。**注意**：MVP_V1 的 `embed-cost-estimate` 算的是 embedding 成本，跟這裡要估的 chat completion／judge 呼叫成本是不同的東西，以 `api-cost-estimate` 為準。
- 此 repo 的文件與 commit 風格說明主要語言為繁體中文（zh-TW）；編輯既有文件時請沿用中文，而非改用英文。
- **「小插播」是優先回答信號**：使用者在任何進行中的任務期間，訊息開頭出現「小插播」三個字，代表這是一個要**優先回答**的問題，不用等手邊任務全部做完才處理。回答方式要**簡單、白話、簡短**，不要長篇大論，答完再視情況接續原本手邊在做的事。
- **想看目前這個對話完整的 context 組成**（包含每個 MCP tool、每個 skill、custom agent 各自佔多少 token），打 `/context 的版面` 就會出現完整面板，會列出每個分類與每個 MCP 工具的實際 token 數與百分比。
