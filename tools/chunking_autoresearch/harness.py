"""tools/chunking_autoresearch/harness.py

固定、不可改的驗證程式（對應 autoresearch 的 prepare.py 角色）。
agent（Hermes）絕對不能編輯這個檔案。

這一版是四方獨立審查（qa / research / code-review / codex）之後的重寫，
修掉的核心問題：
  1. 品質檢查原本只驗證欄位格式，完全沒檢查「內容有沒有被保留下來」——
     QA 實測證明：一個把資料整個丟光的策略反而能拿到比 baseline 更低的
     cost_time。現在加了 content coverage 的硬性門檻（見 MIN_CONTENT_COVERAGE），
     沒過門檻直接给一個遠高於任何正常結果的 cost_time，不再能用「少做事」取巧。
  2. strategy.chunk() 現在透過 `_worker.py` 在獨立 subprocess 裡執行，
     父行程自己的計時器／常數碰不到，也真的有 timeout 強制中止。
  3. 計時範圍排除 PDF 解析（固定成本、策略改不動），並且跑三次取中位數，
     降低單次執行的隨機雜訊。

2026-08-14 補：四方 AI coding agent（Claude Code / Codex / Antigravity /
Copilot）比較實驗中，Codex 把整份文件包成單一個 prose chunk（完全不做段落
／表格拆分），結果 content_coverage 直接等於 1.0、quality_pass_rate 也合法
過關，因為切法本身在結構上仍是合法的 chunk——這是先前 content coverage 這
道關卡沒有堵住的新漏洞：只檢查「內容有沒有保留」，沒檢查「有沒有真的做到
有意義的切分」。加了 MAX_CHUNK_SHARE_OF_DOCUMENT 這道新關卡堵住這個做法。

2026-09-01 補（`_worker.py` 改成常駐迴圈）：累積提升度卡在 14-15% 一段時間
後，用 `python -X importtime` 實測發現原本「每次重複計時都整個重開一個新
subprocess」的做法，重開直譯器＋重新 import 佔掉了 88.6% 的 `cost_time`，
`strategy.chunk()` 自己的邏輯只佔 5.97%——真正的策略改進幅度幾乎完全被這筆
重複繳的固定成本蓋過去。改成一個 subprocess 生命週期內處理 `TIMING_REPEATS`
次請求（見 `_run_worker_session()`），import 成本只付一次；計時方式不變，
仍然是父行程（這裡）用 `time.perf_counter()` 量每次來回的時間，子行程碰不到
這個計時器。**這個改動會讓量到的 `BASELINE_SECONDS`
量級整個下降**（少了重複 import 的固定開銷），部署後第一次「設備基準測量」
量到的 `device_baseline_cost_time` 會比部署前小很多——這是量測架構升級造成
的，不是機器突然變快，program.md 已經有對應的 `REFERENCE_DEVICE_BASELINE`
重新校準規則，不要把這次的落差當成「設備基準異常」處理。

**同一輪 QA 審查另外抓到一個更早就存在的舊漏洞（不是這次新引入，但既然這次
重點就是重新確立「父行程計時、子行程碰不到」這個安全前提，一併修掉）**：
`harness.py` 自己過去有 `import strategy`（只是為了讀 `STRATEGY_NAME` 印進
摘要），但 import 本身就會執行 `strategy.py` 的模組層級程式碼——而這裡就是
父行程自己的 process，`_run_worker_session()` 用的 `time` 模組是
`sys.modules['time']` 這個全 process 共用的單例，只要 `strategy.py` 在模組
層級動一行 monkeypatch，就能直接偽造父行程量到的 `cost_time`（QA 實測證實
可行：偽造出 `cost_time: 0.000000` 還能通過 hard gate）。改成用純文字 regex
從 `strategy.py` 原始碼擷取 `STRATEGY_NAME`（見 `_read_strategy_name()`），
完全不 import、不執行這個檔案裡的任何程式碼，才是真正堵住「父行程碰不到」
這句話唯一還沒堵住的破口。

用法：python harness.py（或 uv run harness.py）
"""

from __future__ import annotations

import json
import queue
import re
import statistics
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from pdf_utils import parse_pdf_pages
from schema import Chunk, PageParseResult

# 2026-09-01 QA 審查抓到：這裡原本 `import strategy` 只是為了讀
# `STRATEGY_NAME` 這個字串常數放進摘要輸出，註解宣稱「不會呼叫
# strategy.chunk()，所以不影響計時或評分的可信度」——這個推論是錯的。
# `import` 本身就會執行 `strategy.py` 的**模組層級程式碼**，而這個 process
# 就是父行程本身，`_run_worker_session()` 拿到的 `time` 模組是
# `sys.modules['time']` 這個全 process 共用的單例——只要 `strategy.py`
# 在模組層級動一行 `sys.modules['time'].perf_counter = ...`，就能直接竄改
# 父行程量到的計時，偽造出無限大的速度提升（已被 QA 實測證實可行，是
# 這次 worker 常駐化改動之前就存在的舊漏洞，不是這次新引入的，但既然
# 這次的重點就是重新確立「父行程計時、子行程碰不到」這個安全前提，
# 這個洞必須一起補起來）。改成用純文字 regex 從 `strategy.py` 的原始碼
# 擷取 `STRATEGY_NAME`，完全不 import、不執行這個檔案的任何程式碼。
_STRATEGY_NAME_RE = re.compile(r'^STRATEGY_NAME\s*=\s*["\'](.+?)["\']\s*$', re.MULTILINE)

# ---------------------------------------------------------------------------
# 固定常數——agent 可以讀，但不能改這個檔案本身
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs" / "spike_documents"
TOOL_DIR = Path(__file__).resolve().parent

DOCUMENTS = [
    "新進人員實習表.pdf",
    "2415-1305研究報告-太陽光發電預測.pdf",
    "2415-1304研究報告-智能貨櫃屋 .pdf",
    "A 完整版本  鋰電池二次利用之電池管理系統開發研究完成報告.pdf",
]

# 每份文件的原始內容，至少要有這個比例透過 shingle 比對出現在 chunk 裡，
# 否則視為硬性失敗（見 content_coverage）。這個門檻是防呆用的，不是拿來
# 精細比較「哪個策略切得比較好」——切得好不好交給 quality_pass_rate。
MIN_CONTENT_COVERAGE = 0.90
SHINGLE_SIZE = 30

# 單一 chunk 不能佔同一份文件超過這個比例的字元數，否則視為硬性失敗。
# 防止「整份文件包成一個 chunk」這種沒有真正做到切分、卻能讓 content
# coverage 直接滿分的取巧做法（2026-08-14 四方 AI 比較實驗中，Codex
# 實際用這招鑽過了原本的關卡，詳見上方檔案說明）。
MAX_CHUNK_SHARE_OF_DOCUMENT = 0.5

# 單一 chunk 的絕對字元數上限，不管佔文件比例多少都不能超過。
# MAX_CHUNK_SHARE_OF_DOCUMENT 單獨存在時，還是能被「把文件無腦對半切成兩塊
# （不管段落／表格邊界）」這招繞過——兩塊各佔 50%，比例上剛好卡在門檻內，
# 但一樣沒有做到「有意義的切分」。這裡的門檻抓 baseline CHUNK_SIZE（600）
# 的約 3 倍，足以擋下「切成兩三塊超大 chunk」這種粗暴做法——本次測試文件
# 字元數是 34910～59702，對半切出來的每塊都會遠遠超過這個上限。
#
# 實際安全邊際比「3 倍」聽起來的要窄：baseline 目前最大的單一 chunk（一個
# 完整表格 chunk，因為表格不會被切開）實測是 1301 字元，離 2000 的門檻只有
# 約 35% 的餘裕，不是 600 到 2000 之間的完整空間（overlap 打包＋表格整塊不
# 拆，讓實際 chunk 大小比 CHUNK_SIZE 本身大不少）。如果之後想讓策略嘗試更大
# 的 CHUNK_SIZE（例如 900 以上），很可能會直接撞上這道門檻，屆時要重新評估
# 這個常數，不是 bug，是設計上已知的取捨。
MAX_CHUNK_CHAR_COUNT = 2000

# 切出來的內容裡，重複收錄的字元 shingle 比例不能超過這個上限，否則視為
# 硬性失敗。前兩道關卡（MAX_CHUNK_SHARE_OF_DOCUMENT／MAX_CHUNK_CHAR_COUNT）
# 擋的是「切太大」；這道關卡擋的是鏡像方向——「切太碎」：把文件切成大量
# 極小、彼此高度重疊的 chunk，一樣能通過前面所有關卡（每個 chunk 格式合法、
# 內容也都保留了），但同樣不是「有意義的切分」。redundancy_ratio 是決定性
# 指標（不受計時雜訊影響，同一個策略重跑幾次數字完全一樣），適合當硬性
# 門檻用。baseline 實測 redundancy_ratio＝0.117077，這裡抓約 3.4 倍當上限，
# 跟 MAX_CHUNK_CHAR_COUNT 的「baseline 的 3 倍」抓法一致（2026-08-14 code
# review 指出這個殘留方向，見下方「補三」）。
MAX_REDUNDANCY_RATIO = 0.4

# 硬性失敗（quality 或 coverage 沒過門檻）的 cost_time 基準值，遠高於任何正常
# 執行結果，讓「投機取巧」永遠競爭不過「認真切但慢一點」。
# dashboard.py 的 `_HARD_GATE_PENALTY_FLOOR` 是同一個數字的獨立副本（拿來判斷
# 「研究進度總覽」圖裡哪些列是懲罰值、該排除在正規化計算外），改這裡記得也去
# 改那邊，兩邊沒有 import 關係、不會自動同步。
HARD_FAILURE_BASE = 1000.0

# baseline 策略（strategy.py 目前的內容）跑出來的中位數秒數，讓
# normalized_seconds 有一個穩定的參考值。如果之後手動調整 baseline，
# 這個常數要跟著手動更新（跑 harness.py 幾次、取中位數）。
BASELINE_SECONDS = 0.11

# 每次實驗跑幾次 strategy，取中位數計時，降低單次執行的隨機雜訊。
TIMING_REPEATS = 3

# 子行程逾時秒數：卡住或跑太久的策略會被強制中止，計入 crash。
SUBPROCESS_TIMEOUT_SECONDS = 30

WORKER_SCRIPT = TOOL_DIR / "_worker.py"
STRATEGY_SCRIPT = TOOL_DIR / "strategy.py"


# ---------------------------------------------------------------------------
# 結構品質檢查（照搬 MVP_V1 spike/run_chunking_comparison.py 的 _metadata_ok）
# ---------------------------------------------------------------------------


def _metadata_ok(chunk: Chunk) -> bool:
    if chunk.page_index_range[0] > chunk.page_index_range[1]:
        return False
    if chunk.char_count != len(chunk.text):
        return False
    if chunk.chunk_type not in ("prose", "table"):
        return False
    if chunk.chunk_type == "table" and not chunk.table_title:
        return False
    if not chunk.text.strip():
        return False
    return True


def _split_table_chunk_ids(chunks: list[Chunk]) -> set[str]:
    """回傳「疑似被腰斬」的 chunk_id 集合：同一份文件裡，緊接著的兩個 chunk
    都是表格類型、且 table_title 相同，代表這其實是同一張表被硬拆成兩塊，
    不是兩張各自完整的表格。"""
    bad_ids: set[str] = set()
    for prev, cur in zip(chunks, chunks[1:]):
        if (
            prev.source_filename == cur.source_filename
            and prev.chunk_type == "table"
            and cur.chunk_type == "table"
            and prev.table_title
            and prev.table_title == cur.table_title
        ):
            bad_ids.add(prev.chunk_id)
            bad_ids.add(cur.chunk_id)
    return bad_ids


def score_chunks(chunks: list[Chunk]) -> float:
    """回傳所有 chunk 裡通過結構檢查的比例，1.0 代表全部通過。結構檢查包含
    每個 chunk 自己的格式（見 _metadata_ok）以及有沒有表格被腰斬（見
    _split_table_chunk_ids）。"""
    if not chunks:
        return 0.0
    split_ids = _split_table_chunk_ids(chunks)
    ok_count = sum(1 for c in chunks if _metadata_ok(c) and c.chunk_id not in split_ids)
    return ok_count / len(chunks)


# ---------------------------------------------------------------------------
# 內容保留率檢查（新增：這是修掉「丟光資料反而分數更高」的關鍵）
# ---------------------------------------------------------------------------


def _shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    normalized = "".join(text.split())
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[i : i + size] for i in range(len(normalized) - size + 1)}


def content_coverage(source_pages: list[PageParseResult], chunks: list[Chunk]) -> float:
    """用固定長度的字元 shingle 比對，估算原始文件內容有多少比例真的出現在
    切出來的 chunk 裡。切法可以重疊、可以重新排列，這些都不影響分數；
    但把內容丟掉不切，分數會明顯下降，無法靠「少做事」騙到高分。
    """
    source_text = "".join(p.text for p in source_pages)
    source_shingles = _shingles(source_text)
    if not source_shingles:
        return 1.0
    chunk_text = "".join(c.text for c in chunks)
    chunk_shingles = _shingles(chunk_text)
    covered = source_shingles & chunk_shingles
    return len(covered) / len(source_shingles)


def max_chunk_share(source_pages: list[PageParseResult], chunks: list[Chunk]) -> float:
    """回傳這份文件裡，單一 chunk 的字元數佔原始文件總字元數的最高比例。

    用比例而非絕對字數，是因為四份測試文件長短差很多，固定字數上限對短文件
    太寬鬆、對長文件太嚴格；用比例才能公平地套用在不同大小的文件上。
    """
    source_char_count = sum(p.char_count for p in source_pages)
    if source_char_count == 0 or not chunks:
        return 0.0
    return max(c.char_count for c in chunks) / source_char_count


def redundancy_ratio(chunks: list[Chunk]) -> float:
    """估算切出來的內容裡，有多少比例是「重複收錄」的。做法：把每個 chunk
    的字元 shingle 攤平算出現次數，總出現次數扣掉「不重複的 shingle 種類數」，
    再除以總出現次數——分子就是「超過第一次出現、算重複」的部分。

    不跟固定字數的重疊量（例如 baseline 目前用的 100 字）比較，因為那只是
    baseline 自己選的參數，不是放諸四海皆準的標準；agent 之後可能用完全不同
    的切法。這裡只回傳「這次切出來的原始重複比例」這個數字本身，交給
    dashboard 拿去跟 baseline 自己的第一筆結果比較（跟 cost_time 的處理方式
    一樣），不在這裡預設「多少重複才算正常」。
    """
    all_shingles: list[str] = []
    for c in chunks:
        all_shingles.extend(_shingles(c.text))
    if not all_shingles:
        return 0.0
    unique_count = len(set(all_shingles))
    return (len(all_shingles) - unique_count) / len(all_shingles)


# ---------------------------------------------------------------------------
# 透過 subprocess 執行 strategy.chunk()（隔離 + 真正的 timeout）
# ---------------------------------------------------------------------------


def _read_line_with_timeout(stream: TextIO, timeout: float) -> Optional[str]:
    """從一個可能會卡住的 pipe 讀一行，最多等 `timeout` 秒。標準函式庫的
    pipe 物件沒有內建的「讀一行、逾時就放棄」功能，且 `select`/`selectors`
    在 Windows 上不支援一般的檔案／pipe（只支援 socket），所以用一個
    daemon thread 去做真正的阻塞讀取，主執行緒用 `queue.get(timeout=...)`
    等結果——這個寫法跨平台都能用，逾時沒讀到就回傳 None，讀取執行緒會在
    stream 關閉或 process 被父行程強制結束後自然退出，不用手動中斷它。"""
    result: "queue.Queue[str]" = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            line = stream.readline()
        except (OSError, ValueError):
            line = ""
        result.put(line)

    threading.Thread(target=_reader, daemon=True).start()
    try:
        return result.get(timeout=timeout)
    except queue.Empty:
        return None


def _drain_stderr(stream: TextIO, out: list[str]) -> None:
    """持續、非阻塞地把子行程的 stderr 讀進 `out`，在背景 thread 執行，跟主
    迴圈平行跑（不是等全部重複測完才一次讀）。OS 的 pipe buffer 有限（常見
    預設約 64KB）：如果只在最後才讀 stderr，子行程一旦寫了夠多內容（不只是
    crash 的 traceback，一般的 debug print 也算）就會卡在寫入上，連帶讓對應
    那次 stdout 讀取逾時、把整個 session 誤判成失敗——用一個持續在背景吸收
    輸出的 thread 才能避免 pipe 被塞滿卡住。"""
    for line in iter(stream.readline, ""):
        out.append(line)


def _run_worker_session(
    payload: list[dict], repeats: int, timeout: float
) -> tuple[Optional[list[dict]], list[float]]:
    """啟動「一個」常駐的 `_worker.py` subprocess，重複送 `repeats` 次同一份
    payload，回傳 (第一次成功回覆的文件/chunk 清單, 每次來回各自量到的秒數
    清單)。任何一次來回逾時、subprocess 提前結束、或回傳的不是合法 JSON，
    直接視為這次 session 失敗，回傳 (None, 目前已經量到的秒數清單)——呼叫端
    看 `len(timings) < repeats` 或 `worker_output is None` 就知道失敗了，
    跟舊版「任何一次重複 crash 就整個當掉」的語意一致，不是新的行為。**每一次
    來回收到的回覆都會各自驗證是合法 JSON**（不是只驗證第一次），確保「重複
    `repeats` 次的每一次都真的是有效回應」這個不變量沒有被弱化——萬一某次
    重複因為模組層級狀態污染等原因回傳壞掉的內容，那次連同它的計時會被整個
    session 判定失敗，不會被安靜地算進中位數。

    2026-09-01 改成常駐 subprocess（原本是每次重複測時都整個重開一個新
    subprocess）：實測發現直譯器重開＋重新 import `re`／`schema.py`（連帶
    `dataclasses`／`inspect`／`typing`）佔掉了原本量到的 `cost_time` 的
    88.6%，`strategy.chunk()` 本身只佔 5.97%，把策略真正的進步幅度整個蓋
    過去。改成一個 subprocess 生命週期只付一次 import 成本，重複測時共用
    已經 import 好的模組。**計時的人仍然是這裡（父行程）**：每一次來回都是
    父行程自己用 `time.perf_counter()` 算「送出請求到收到回覆」的時間，
    子行程只負責處理、完全不接觸這個計時器，跟改之前的安全隔離性質相同
    ——詳見 `_worker.py` 檔案開頭的說明。"""
    import time

    payload_line = json.dumps(payload) + "\n"
    timings: list[float] = []
    worker_output: Optional[list[dict]] = None
    stderr_lines: list[str] = []
    with subprocess.Popen(
        [sys.executable, str(WORKER_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(TOOL_DIR),
    ) as proc:
        assert proc.stderr is not None
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr, stderr_lines), daemon=True)
        stderr_thread.start()
        try:
            for _ in range(repeats):
                start = time.perf_counter()
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(payload_line)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
                assert proc.stdout is not None
                line = _read_line_with_timeout(proc.stdout, timeout)
                elapsed = time.perf_counter() - start
                if not line:
                    break
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    break
                timings.append(elapsed)
                if worker_output is None:
                    worker_output = parsed
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr_thread.join(timeout=timeout)

    if len(timings) < repeats or worker_output is None:
        stderr_text = "".join(stderr_lines)
        if stderr_text:
            print(stderr_text, file=sys.stderr)
        return None, timings
    return worker_output, timings


def _chunk_from_dict(d: dict) -> Chunk:
    d = dict(d)
    d["page_index_range"] = tuple(d["page_index_range"])
    d["pdf_page_number_range"] = tuple(d["pdf_page_number_range"])
    return Chunk(**d)


def _read_strategy_name() -> str:
    """純文字讀取 `strategy.py` 的原始碼、用 regex 找 `STRATEGY_NAME = "..."`
    這一行，完全不 import、不執行這個檔案裡的任何程式碼——見上方 import 區塊
    的說明，這是為了避免 `strategy.py` 的模組層級程式碼在父行程裡跑，有機會
    竄改父行程自己的 `time` 模組。找不到就回傳 `"unnamed"`，不當成錯誤。"""
    try:
        text = STRATEGY_SCRIPT.read_text(encoding="utf-8")
    except OSError:
        return "unnamed"
    match = _STRATEGY_NAME_RE.search(text)
    return match.group(1) if match else "unnamed"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run() -> None:
    # PDF 解析是固定成本、策略改不動它，排除在計時範圍之外。
    pages_by_doc: dict[str, list[PageParseResult]] = {
        filename: parse_pdf_pages(str(DOCS_DIR / filename)) for filename in DOCUMENTS
    }
    payload = [
        {
            "filename": filename,
            "pages": [
                {
                    "page_index": p.page_index,
                    "pdf_page_number": p.pdf_page_number,
                    "text": p.text,
                    "char_count": p.char_count,
                }
                for p in pages
            ],
        }
        for filename, pages in pages_by_doc.items()
    ]

    worker_output, timings = _run_worker_session(payload, TIMING_REPEATS, SUBPROCESS_TIMEOUT_SECONDS)
    if worker_output is None or len(timings) < TIMING_REPEATS:
        print("strategy.chunk() 在 subprocess 裡失敗或逾時（詳見上方 stderr）", file=sys.stderr)
        sys.exit(1)
    all_chunks: list[Chunk] = []
    coverage_scores: list[float] = []
    chunk_share_scores: list[float] = []
    for doc in worker_output:
        filename = doc["filename"]
        chunks = [_chunk_from_dict(c) for c in doc["chunks"]]
        all_chunks.extend(chunks)
        coverage_scores.append(content_coverage(pages_by_doc[filename], chunks))
        chunk_share_scores.append(max_chunk_share(pages_by_doc[filename], chunks))

    quality_pass_rate = score_chunks(all_chunks)
    avg_content_coverage = sum(coverage_scores) / len(coverage_scores) if coverage_scores else 0.0
    avg_redundancy_ratio = redundancy_ratio(all_chunks)
    worst_chunk_share = max(chunk_share_scores) if chunk_share_scores else 0.0
    worst_chunk_char_count = max((c.char_count for c in all_chunks), default=0)
    median_seconds = statistics.median(timings)
    normalized_seconds = median_seconds / BASELINE_SECONDS if BASELINE_SECONDS > 0 else median_seconds

    hard_gate_passed = (
        quality_pass_rate >= 1.0
        and avg_content_coverage >= MIN_CONTENT_COVERAGE
        and worst_chunk_share <= MAX_CHUNK_SHARE_OF_DOCUMENT
        and worst_chunk_char_count <= MAX_CHUNK_CHAR_COUNT
        and avg_redundancy_ratio <= MAX_REDUNDANCY_RATIO
    )
    if hard_gate_passed:
        cost_time = normalized_seconds
    else:
        cost_time = (
            HARD_FAILURE_BASE
            + (1 - avg_content_coverage) * 100
            + (1 - quality_pass_rate) * 100
            + max(0.0, worst_chunk_share - MAX_CHUNK_SHARE_OF_DOCUMENT) * 100
            + max(0.0, (worst_chunk_char_count - MAX_CHUNK_CHAR_COUNT) / MAX_CHUNK_CHAR_COUNT) * 100
            + max(0.0, avg_redundancy_ratio - MAX_REDUNDANCY_RATIO) * 100
        )

    print("---")
    print(f"cost_time:         {cost_time:.6f}")
    print(f"quality_pass_rate: {quality_pass_rate:.6f}")
    print(f"content_coverage:  {avg_content_coverage:.6f}")
    print(f"redundancy_ratio:  {avg_redundancy_ratio:.6f}")
    print(f"max_chunk_share:   {worst_chunk_share:.6f}")
    print(f"max_chunk_chars:   {worst_chunk_char_count}")
    print(f"hard_gate_passed:  {hard_gate_passed}")
    print(f"seconds:           {median_seconds:.6f}")
    print(f"baseline_seconds:  {BASELINE_SECONDS:.6f}")
    print(f"num_chunks:        {len(all_chunks)}")
    print(f"strategy_name:     {_read_strategy_name()}")
    # 這裡只是 stdout 摘要的列印順序，Hermes 用 grep "^timestamp:" 這種方式逐欄位
    # 取值，不依賴印出的順序。results.tsv 實際的欄位順序是另一份獨立定義，見
    # program.md「記錄結果」章節（2026-08-14 起 timestamp 在 tsv 裡固定第一欄、
    # description 固定最後一欄，新增欄位一律要連同歷史資料一起 migrate，不能
    # 再只是單純附加在最後面）。
    print(f"timestamp:         {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    run()
