# AI Benchmark and Evaluation Platform

## 這是什麼

這是一個獨立專案，目標是建立一套可長期沿用的 **AI Evaluation / Benchmarking 方法論與工具**，用來系統化判斷不同 AI 模型、RAG、Agent 是否值得導入專案。


## 與 AI Energy Operations Copilot (MVP_V1) 的關係

本專案**不是** MVP_V1 的一部分，是獨立資料夾，避免兩個專案的檔案互相混雜。但兩者關係緊密：

- MVP_V1 是「被評估對象」（system under test）——它的 RAG pipeline、`/assistant` chat、tool orchestration 都可以是這裡的評測目標。
- `datasets/` 底下的起始測試資料是從 MVP_V1 的 `spike/` 複製過來的既有驗證資產（**複製，不是搬移**——MVP_V1 自己的測試與腳本仍依賴原始檔案，這裡的副本只作為 Evaluation Platform 的起始資料，兩邊之後會各自演進，不會互相同步）。

## 資料夾結構

```text
AI Benchmark and Evaluation Platform/
├── docs/                                          # 研究筆記與報告
├── datasets/                                      # 測試資料（含從 MVP_V1 複製的起始資料）
│   ├── energyops_test_questions.json              # 29 題，複製自 MVP_V1 spike/test_questions.json
│   ├── energyops_retrieval_benchmark_baseline.json # 複製自 MVP_V1，作為 baseline 數字參考
│   └── energyops_chunking_comparison_baseline.json
└── results/                                        # 之後每次評測跑出來的結果放這裡
```

## 目前進度

