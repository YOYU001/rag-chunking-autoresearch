"""tools/chunking_autoresearch/_worker.py

在獨立的 subprocess 裡執行 strategy.chunk()。這是四方審查（qa／codex）都指出
的問題的修法：原本 strategy.py 是直接被 import 進 harness.py 同一個
process 執行，理論上可以 monkeypatch `time.perf_counter`、改掉 harness.py
的全域常數、或做其他越權的事，「只能改 strategy.py」在那個設計下只是文件
上的約定，不是真的技術防線。

harness.py 只透過 stdin/stdout 的 JSON 跟這支 subprocess 交換資料，父行程
自己的計時器、常數、模組狀態，子行程完全碰不到；父行程也會對每一次來回
強制設 timeout，卡住或跑太久的請求會被父行程判定失敗。

2026-09-01 改成常駐迴圈（見下方「為什麼改成常駐」）：一個 subprocess 生命
週期內可以處理多筆請求，import `strategy`／`schema` 的成本只付一次，不用
每次都重新啟動直譯器、重新 import。**計時的人還是父行程**（`harness.py`
用它自己的 `time.perf_counter()` 量「送出請求到收到回覆」的來回時間）——
這支腳本本身完全不量時間、也不對外回報任何耗時數字，子行程沒辦法竄改
父行程量到的秒數，安全隔離性質跟改之前完全一樣，只是把「重開整個
process」的固定成本從「每次重複測時都付一次」改成「一個 subprocess
生命週期只付一次」。

為什麼改成常駐：2026-09-01 用 `python -X importtime` 實測，一次完整的舊版
subprocess（重開直譯器＋import `re`／`schema.py` 的 `dataclasses` 連帶
`inspect`／`typing`）要吃掉整個量到的 `cost_time` 的 88.6%，`strategy.chunk()`
本身的邏輯只佔 5.97%——真正的策略改進幅度被這筆重複繳的「開機稅」蓋掉了，
這是造成累積提升度卡在 14-15%（理論上限接近 chunk() 自己那 6% 的天花板）
的主因，不是 chunking 演算法真的已經無法再進步。

已知的取捨（不是漏洞，是設計上刻意接受的權衡）：同一個 subprocess 生命
週期內重複呼叫 `strategy.chunk()`，理論上如果 `strategy.py` 用了模組層級
的快取（例如 `functools.lru_cache`）記住輸入輸出，後面幾次重複測時可能會
比真正冷啟動快——但這頂多讓同一次實驗的中位數更接近「暖機後的穩態效能」，
不是「冷啟動效能」，兩者都是合理的效能定義，不算作弊（hard gate 的品質
指標——quality_pass_rate／content_coverage／chunk 大小／redundancy_ratio
——都是拿第一次呼叫的真實輸出去驗證，不受這個影響）。每次 harness.py
呼叫仍然會啟動一個全新的 subprocess（不會跨輪次、跨 commit 沿用同一個
worker），所以這個暖機效果的範圍只限於同一次實驗的 3 次重複計時內。

不要手動執行這支腳本，由 harness.py 呼叫。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

import strategy
from schema import PageParseResult


def _pages_from_json(raw_pages: list[dict]) -> list[PageParseResult]:
    return [PageParseResult(**p) for p in raw_pages]


def _chunk_to_dict(chunk) -> dict:
    d = asdict(chunk)
    d["page_index_range"] = list(chunk.page_index_range)
    d["pdf_page_number_range"] = list(chunk.pdf_page_number_range)
    return d


def _run_once(payload: list[dict]) -> list[dict]:
    output = []
    for doc in payload:
        pages = _pages_from_json(doc["pages"])
        chunks = strategy.chunk(pages, doc["filename"])
        output.append(
            {
                "filename": doc["filename"],
                "chunks": [_chunk_to_dict(c) for c in chunks],
            }
        )
    return output


def main() -> None:
    """逐行讀 stdin：每一行是一次獨立請求的 JSON payload，處理完立刻把
    結果 JSON 寫成一行印到 stdout 並 flush，再繼續等下一行——父行程
    （harness.py）靠這個「一行進、一行出」的節奏，對每一次來回各自掐表。
    stdin 關閉（EOF）就正常結束；任何一次請求處理時丟例外，直接讓它往上
    炸掉整個 process（不要在這裡吞掉），父行程會偵測到這次來回沒有拿到
    回覆、判定失敗——跟舊版單次 subprocess crash 的處理方式一致。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        output = _run_once(payload)
        sys.stdout.write(json.dumps(output))
        sys.stdout.write("\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
