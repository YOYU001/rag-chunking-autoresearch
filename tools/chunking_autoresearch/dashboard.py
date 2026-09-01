"""tools/chunking_autoresearch/dashboard.py

NAS 上常駐的即時儀表板：讀 `results.tsv`，畫出四個指標（速度提升度／組裝正確率／
內容保留率／內容擷取範圍精確度）隨時間變化的趨勢圖，外加距離 20% 通知門檻還差多少
的進度條。純標準函式庫實作，不加新的相依套件——圖表用 inline SVG 手動畫，不需要
matplotlib，日期篩選、指標切換都靠 GET query string 讓伺服器重新算，瀏覽器端只有
搜尋歷史紀錄跟亮暗模式切換兩個小功能用原生 JS，沒有任何前端框架或建置流程。

用瀏覽器打開 http://127.0.0.1:8765/ 看。原本是每 5 秒自動整頁重新整理，但圖表變寬、
變複雜之後這樣會很明顯地閃爍，改成不自動整理——`results.tsv` 每次請求都會重新讀取，
用瀏覽器自己的重新整理（會保留目前的 URL query string，包含篩選中的日期範圍跟選定
的指標）就能看到 Hermes 背景寫入的最新結果，不用另外做一個按鈕。

`results.tsv` 裡的每一筆紀錄都會保留（包含 discard／crash），只有失敗嘗試的
`strategy.py` 程式碼本身會被 `program.md` 的流程用 `git reset` 丟掉——log 保留
完整歷史，才能在儀表板上看到「失敗過幾次、後來怎麼找到對的方向」的完整趨勢，不是
只看到一路向上的成功曲線。

四個指標的實際定義（跟 `harness.py` 對齊，避免望文生義；命名跟拆分方式參考了
`docs/rag_chunking_evaluation_metrics_research.md` 的研究整理）：
- 速度提升度（來自 `cost_time`）：跟 baseline 比，這次跑得快了幾 %。原始
  `cost_time` 數字是「越低越好」，這裡換算成「提升 %」用正向、越高越好的方式呈現。
- 組裝正確率（來自 `quality_pass_rate`）：每個 chunk 自己的格式對不對（分頁範圍、
  字數、表格標題等），加上表格有沒有被腰斬（同一張表被硬拆成兩個 chunk）的檢查。
  kept 紀錄規定一定要 100% 過關才留得下來，所以這條線幾乎都是一直線 100%，是正常
  現象，不代表沒資料或圖表壞掉。
- 內容保留率（來自 `content_coverage`）：測的是程式邏輯錯誤誤丟的資料比例——原始
  文件的內容有多少比例還留在切出來的 chunk 裡，誤丟越多這個數字越低。只抓「有沒有
  丟資料」，不抓「有沒有拿太多、重複、灌水」，那是下面內容擷取範圍精確度的工作。
- 內容擷取範圍精確度（來自 `redundancy_ratio`）：跟 baseline 比，chunk 之間重複／
  灌水的內容減少了幾 %。原始 `redundancy_ratio` 只是「這次切出來的重複比例」，沒有
  固定門檻，因為切法不同、正常重疊量本來就不一樣，只跟 baseline 自己的數字比高低，
  跟速度提升度是同一套處理邏輯。

用法：python dashboard.py
"""

from __future__ import annotations

import csv
import html
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

TAIWAN_TZ = timezone(timedelta(hours=8))
RESULTS_TSV = Path(__file__).resolve().parent / "results.tsv"
PORT = 8765
NOTIFY_THRESHOLD_PCT = 20.0  # 要跟 notify.py 的 IMPROVEMENT_THRESHOLD_PCT 對齊
HISTORY_ROW_LIMIT = 500  # 歷史紀錄表最多顯示幾筆（防呆上限，不是預期常態值）；
# 超過 30 筆之後表格本身會在固定高度的框裡捲動，不會把整個頁面越撐越長

SIDEBAR_ITEMS = [
    ("dashboard", "Dashboard", True),
    ("chat", "Chat", False),
    ("user", "User", False),
    ("board", "Board", False),
    ("database", "資料庫查看", False),
    ("setting", "Setting", False),
]


# ---------------------------------------------------------------------------
# 讀資料 / 解析
# ---------------------------------------------------------------------------


def _read_rows() -> list[dict]:
    if not RESULTS_TSV.exists():
        return []
    with RESULTS_TSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _safe_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _enrich(rows: list[dict]) -> list[dict]:
    """幫每一列補上 parse 過的數字欄位跟時間戳記，缺 timestamp 的舊資料
    （改欄位之前寫進去的紀錄）ts 會是 None，之後一律當「未知日期」處理。"""
    out = []
    for r in rows:
        out.append(
            {
                **r,
                "_cost_time": _safe_float(r.get("cost_time")),
                "_quality_pass_rate": _safe_float(r.get("quality_pass_rate")),
                "_content_coverage": _safe_float(r.get("content_coverage")),
                "_redundancy_ratio": _safe_float(r.get("redundancy_ratio")),
                "_seconds": _safe_float(r.get("seconds")),
                "_same_session_baseline_cost_time": _safe_float(r.get("same_session_baseline_cost_time")),
                "_device_baseline_cost_time": _safe_float(r.get("device_baseline_cost_time")),
                "_device_anomaly_note": r.get("device_anomaly_note") or "",
                "_ts": _parse_ts(r.get("timestamp")),
            }
        )
    return out


def _to_taiwan(ts: datetime) -> datetime:
    """只有帶時區資訊（aware）的時間才能安全轉換，沒有時區資訊的舊資料維持原樣。"""
    return ts.astimezone(TAIWAN_TZ) if ts.tzinfo is not None else ts


def _fmt_date(ts: Optional[datetime]) -> str:
    """統一換算成台灣時間再顯示。harness.py 正常寫入的 timestamp 是 UTC（結尾 +00:00，
    來自 `datetime.now(timezone.utc).isoformat()`），但少數用 `git log --format=%cI`
    補記的舊資料是本地時間（結尾 +08:00）——兩種格式如果不先統一轉成同一個時區就直接
    印出來，會出現同一份歷史紀錄裡的時間互相對不齊、甚至讓人誤判成完全不合理的時間點
    （例如把 UTC 的下午誤看成凌晨）。"""
    if ts is None:
        return "未知日期"
    return _to_taiwan(ts).strftime("%Y-%m-%d %H:%M")


def _filter_by_date(rows: list[dict], start: str, end: str) -> list[dict]:
    """沒有選日期範圍時回傳全部（含未知日期的舊資料）；一旦選了任一邊，未知日期
    的舊資料就篩不到（沒有時間可以比對），這是使用者確認過的行為。"""
    if not start and not end:
        return rows
    filtered = []
    for r in rows:
        ts = r["_ts"]
        if ts is None:
            continue
        d = _to_taiwan(ts).date().isoformat()
        if start and d < start:
            continue
        if end and d > end:
            continue
        filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# 指標定義
# ---------------------------------------------------------------------------


def _first_value(rows: list[dict], key: str) -> Optional[float]:
    """從（未經日期篩選的）完整歷史裡找第一筆有值的紀錄，當作 baseline 用。
    務必傳未篩選過的 rows 進來——如果傳篩選後的列表，日期篩選一旦排除掉真正的
    第一筆實驗，篩選後的第一筆會被誤當成新的 baseline，導致同一筆實驗在篩選
    前後顯示不同的進步幅度（QA／codex 審查抓到的問題）。"""
    return next((r[key] for r in rows if r.get(key) is not None), None)


def _rebaseline_lookup(all_rows: list[dict]) -> dict[str, dict]:
    """把 `status == "rebaseline"` 的紀錄整理成 `commit -> {cost_time,
    device_baseline_cost_time}` 的查詢表。這些是 2026-08-14「設備基準」機制上線
    時，對每一筆歷史 `keep` 紀錄在同一天重新測一次得到的公平對照數字，不是新的
    實驗嘗試，不計入 keep/discard/crash 統計，也不會出現在趨勢圖的線上——只當
    查詢表用，給沒有自己 `device_baseline_cost_time` 的舊 `keep` 紀錄拿去對照。"""
    lookup: dict[str, dict] = {}
    for r in all_rows:
        if r.get("status") != "rebaseline":
            continue
        commit = r.get("commit")
        cost_time = r.get("_cost_time")
        device_baseline = r.get("_device_baseline_cost_time")
        if commit and cost_time is not None and device_baseline is not None:
            lookup[commit] = {"cost_time": cost_time, "device_baseline_cost_time": device_baseline}
    return lookup


def _reference_device_baseline(all_rows: list[dict]) -> Optional[float]:
    """史上第一筆有填 `device_baseline_cost_time` 的紀錄，當作往後所有「設備基準
    正規化」計算的固定參考值。務必傳未篩選過的 rows 進來，理由同 `_first_value`。"""
    return _first_value(all_rows, "_device_baseline_cost_time")


def _same_session_pct(r: dict) -> Optional[float]:
    """回傳這一筆「同時段重測基準」跟「這次 cost_time」比較出來的 %，
    沒有資料就回傳 None。跟最原始 baseline 比的累積 % 會受機器負載波動
    影響失真，這個數字排除了機器負載的干擾，比較準確反映單一這次改動
    實際的效果。"""
    base = r.get("_same_session_baseline_cost_time")
    cur = r.get("_cost_time")
    if base is None or cur is None or base == 0:
        return None
    return (base - cur) / base * 100


def _same_session_note(r: dict) -> str:
    """跟最原始 baseline 比的累積 % 會受機器負載波動影響失真（同一份程式碼，
    機器忙的時候跑起來就是比較慢，看起來像退步）。Hermes 每輪決定 keep/discard
    時，其實有另外在同一個時間窗口重新測過 attempt_base 的版本、排除掉機器負載
    的干擾，這裡把那個「同時段公平比較」的數字顯示出來，比跟很久以前的歷史
    baseline 比更準確反映「這次改動」實際的效果。舊資料沒有這個欄位就不顯示，
    不強迫補資料。"""
    base = r.get("_same_session_baseline_cost_time")
    cur = r.get("_cost_time")
    pct = _same_session_pct(r)
    if pct is None:
        return ""
    return f"\n同時段重測基準：{base:.4f} → 本次：{cur:.4f}（{pct:+.1f}%，已排除機器負載雜訊）"


def _speed_improvement_series(
    kept: list[dict],
    reference_device_baseline: Optional[float],
    rebaseline_lookup: dict[str, dict],
) -> list[Optional[float]]:
    """跟原始 baseline 直接比原始 `cost_time` 的舊算法，會被機器負載跨日波動污染
    ——同一份程式碼，機器忙的時候測起來就是比較慢，看起來像退步，即使程式碼其實
    一直在進步（2026-08-14 使用者發現速度提升度曾經顯示 -107% 這種不合理的大
    負數，根源就在這裡）。曾經試過把每輪的同時段改善倍率逐輪串接相乘，但串接的
    起點本身（最後一筆還沒有同時段資料的舊 keep）就已經是被污染的舊算法算出來的
    數字，污染會被一路帶下去，串接後只從 -107% 變成 -83%，問題沒有真正解決
    （已本機測試驗證），所以放棄這個做法。

    改成「設備基準正規化」：每個 session 開始時都會重測一次最原始 baseline 的
    程式碼，量出這次的 `device_baseline_cost_time`（見 program.md「開始每次執行
    前：設備基準測量」）。用 `cost_time × 參考設備基準 ÷ 這次的設備基準`，把每一筆
    的原始秒數換算成「如果在參考設備狀態下會測到幾秒」，天生互相可比，不需要
    分段、不需要串接：

        normalized_cost_time = cost_time * reference_device_baseline / device_baseline_cost_time
        提升度 % = (reference_device_baseline - normalized_cost_time) / reference_device_baseline * 100

    這個機制上線之前的舊 keep 紀錄，自己沒有 `device_baseline_cost_time`——改用
    `rebaseline_lookup`（今天一次性回測每個歷史 commit 得到的查詢表）拿它們今天
    重新測出來的數字代入，一樣套用同一條公式，沒有特殊分支。"""
    out: list[Optional[float]] = []
    for r in kept:
        cost_time = r.get("_cost_time")
        device_baseline = r.get("_device_baseline_cost_time")
        if (cost_time is None or device_baseline is None) and r.get("commit") in rebaseline_lookup:
            backfilled = rebaseline_lookup[r["commit"]]
            cost_time = backfilled["cost_time"]
            device_baseline = backfilled["device_baseline_cost_time"]
        if (
            cost_time is None
            or device_baseline is None
            or device_baseline == 0
            or reference_device_baseline is None
            or reference_device_baseline == 0
        ):
            out.append(None)
            continue
        normalized_cost_time = cost_time * reference_device_baseline / device_baseline
        out.append((reference_device_baseline - normalized_cost_time) / reference_device_baseline * 100)
    return out


def _normalized_pct_any(
    r: dict,
    reference_device_baseline: Optional[float],
    rebaseline_lookup: dict[str, dict],
) -> Optional[float]:
    """跟 `_speed_improvement_series` 同一條公式，但不限定只吃 `status == "keep"`
    的列——「研究進度總覽」那張圖需要把 discard／crash 的原始資料點也一起畫出來
    當背景灰點，用同一套正規化公式才能跟 keep 的點畫在同一個 y 軸上比較。舊資料
    （2026-08-25 設備基準機制上線之前）如果自己沒有 `device_baseline_cost_time`、
    `commit` 也沒有對應的 `rebaseline` 回測紀錄（`rebaseline` 只回測過歷史上的
    `keep` commit，沒有回測過 discard／crash 的 commit），就沒有公平可比的正規化
    數字，回傳 `None`，呼叫端要略過這種點，不要拿沒正規化過的原始秒數硬畫上去。"""
    cost_time = r.get("_cost_time")
    device_baseline = r.get("_device_baseline_cost_time")
    if (cost_time is None or device_baseline is None) and r.get("commit") in rebaseline_lookup:
        backfilled = rebaseline_lookup[r["commit"]]
        cost_time = backfilled["cost_time"]
        device_baseline = backfilled["device_baseline_cost_time"]
    if (
        cost_time is None
        or device_baseline is None
        or device_baseline == 0
        or reference_device_baseline is None
        or reference_device_baseline == 0
    ):
        return None
    normalized_cost_time = cost_time * reference_device_baseline / device_baseline
    return (reference_device_baseline - normalized_cost_time) / reference_device_baseline * 100


def _speed_growth_vs_best(kept: list[dict], series: list[Optional[float]]) -> Optional[tuple[str, str]]:
    """速度提升度小字徽章：跟「目前為止最好的那一筆 keep」比，不是只跟上一筆
    比——上一筆不一定是目前最好的，跟「目前最好」比才能看出這次改動有沒有
    真的創新高，並在後面附上那筆最佳紀錄的日期方便對照。資料不足兩筆就回傳
    None，讓呼叫端 fallback 回原本的邏輯。"""
    pairs = [(r, v) for r, v in zip(kept, series) if v is not None]
    if len(pairs) < 2:
        return None
    _, latest_v = pairs[-1]
    history = pairs[:-1]
    best_row, best_v = max(history, key=lambda pair: pair[1])
    growth = latest_v - best_v
    date_str = _fmt_date(best_row.get("_ts"))[:10] if best_row.get("_ts") else "未知日期"
    growth_str = f"{growth:+.1f}%（vs {date_str} 最佳）"
    growth_class = "up" if growth >= 0 else "down"
    return growth_str, growth_class


def _coverage_baseline(all_kept: list[dict]) -> Optional[float]:
    v = _first_value(all_kept, "_content_coverage")
    return None if v is None else (1 - v) * 100


def _coverage_improvement_series(kept: list[dict], baseline: Optional[float]) -> list[Optional[float]]:
    """content_coverage 原始值是「保留率」，但概念上這裡真正在追蹤的是「誤丟率」
    （= 1 - 保留率，越低越好）。用跟 baseline 比「差幾個百分點」（直接相減），不是
    「差幾成」（除以 baseline 算比例）——因為 baseline 的誤丟率通常非常接近 0，
    除法算比例會把一點點差距放大成離譜的數字（例如從 0.04% 變成 0.5%，算比例會
    變成「差了1096%」），改成百分點的直接相減就穩定、看得懂多了。"""
    out = []
    for r in kept:
        v = r["_content_coverage"]
        if v is None or baseline is None:
            out.append(None)
        else:
            out.append(baseline - (1 - v) * 100)
    return out


def _redundancy_baseline(all_kept: list[dict]) -> Optional[float]:
    return _first_value(all_kept, "_redundancy_ratio")


def _redundancy_improvement_series(kept: list[dict], baseline: Optional[float]) -> list[Optional[float]]:
    """redundancy_ratio 原始值是「重複比例」，越低越好；這裡跟 baseline 自己的
    第一筆結果比較，換算成「重複減少了幾 %」的正向數字，跟速度提升度同一套處理
    方式——不跟固定門檻比，只跟 baseline 自己比。"""
    out = []
    for r in kept:
        v = r["_redundancy_ratio"]
        if v is None or baseline is None or baseline == 0:
            out.append(None)
        else:
            out.append((baseline - v) / baseline * 100)
    return out


METRICS = {
    # cost_time 沒有填 "baseline_fn"／"series_fn"（下面其他指標都有）：它需要
    # rebaseline 查詢表跟參考設備基準這些額外資料，改在 render_page() 裡特別
    # 處理，不走下面這個通用迴圈，見那邊的註解。
    "cost_time": {
        "label": "速度提升度",
        "desc": (
            "跟最原始 baseline 比，累積快了幾 %（正向表示，越高越好）。2026-08-14 起改用"
            "「設備基準正規化」：每次 Hermes 開始執行前都會重測一次最原始 baseline 的程式碼，"
            "把每一筆的秒數換算成「如果在參考設備狀態下會測到幾秒」再比較，排除機器負載跨天"
            "波動的干擾（不再是直接拿原始秒數相減）。小字徽章是跟「目前為止最好的那一筆」比，"
            "不是只跟上一筆比。"
        ),
        "unit": "%",
        "higher_is_better": True,
        "color": "#4f8cff",
        "baseline_relative": True,
    },
    "quality_pass_rate": {
        "label": "組裝正確率",
        "desc": (
            "每個 chunk 自己的格式對不對（分頁範圍／字數／表格標題）以及表格有沒有被腰斬的檢查。"
            "kept 紀錄規定一定要 100% 過關才留得下來，所以這條線幾乎都是一直線 100%——"
            "這是正常現象，代表沒有任何一筆留下來的結果組裝出錯；真正因組裝錯誤被刷掉的嘗試，"
            "會出現在下面的 discard 歷史紀錄，不會畫進這張圖。"
        ),
        "unit": "%",
        "higher_is_better": True,
        "color": "#8a7cff",
        "baseline_fn": lambda all_kept: None,
        "series_fn": lambda kept, baseline: [
            None if r["_quality_pass_rate"] is None else r["_quality_pass_rate"] * 100 for r in kept
        ],
    },
    "content_coverage": {
        "label": "內容保留率",
        "desc": (
            "卡片大數字是原始絕對保留率（最新一筆 keep 實際測到的比例，100% 代表完全沒丟東西，"
            "跟「組裝正確率」卡片同一種呈現方式）。下面小字徽章跟折線圖走的是另一套算法：跟 "
            "baseline 比，誤丟率少了幾個百分點（正向表示，越高越好——用百分點直接相減，不是算"
            "比例，因為 baseline 的誤丟率通常非常接近 0，算比例會被一點點差距放大成離譜的數字）。"
        ),
        "unit": " 個百分點",
        "higher_is_better": True,
        "color": "#f2b134",
        "baseline_relative": True,
        "baseline_fn": _coverage_baseline,
        "series_fn": _coverage_improvement_series,
    },
    "redundancy_ratio": {
        "label": "內容擷取範圍精確度",
        "desc": "跟 baseline 比，chunk 之間重複／灌水的內容減少了幾 %（正向表示，越高越好）。",
        "unit": "%",
        "higher_is_better": True,
        "color": "#2fbf8f",
        "baseline_relative": True,
        "baseline_fn": _redundancy_baseline,
        "series_fn": _redundancy_improvement_series,
    },
}


# ---------------------------------------------------------------------------
# SVG 圖表
# ---------------------------------------------------------------------------


def _mini_sparkline_svg(values: list[float], color: str, width: int = 220, height: int = 64) -> str:
    if not values:
        return '<svg viewBox="0 0 220 64" width="100%" height="64"></svg>'
    display = values * 2 if len(values) == 1 else values
    lo, hi = min(display), max(display)
    span = hi - lo or 1.0
    n = len(display)
    step = width / max(n - 1, 1)
    pad = 6
    plot_h = height - pad * 2
    coords = [(i * step, pad + plot_h - ((v - lo) / span) * plot_h) for i, v in enumerate(display)]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" preserveAspectRatio="none">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" '
        f'stroke-linejoin="round" points="{polyline}" />'
        f"</svg>"
    )


def _big_chart_svg(
    points: list[dict],
    higher_is_better: bool,
    unit: str,
    accent_color: str,
    grad_id: str,
    baseline_relative: bool = False,
    width: int = 760,
    height: int = 320,
) -> str:
    """points: [{"value": float, "x_label": str, "commit": str, "description": str}, ...]"""
    if not points:
        return '<p class="empty">（還沒有資料）</p>'

    # 左邊留白要跟著單位文字長度變，不然「個百分點」這種長單位的刻度數字會
    # 跟左側直書的座標軸標題疊在一起（原本固定 60px 是抓「%」「s」這種短單位）。
    left_pad, top_pad, right_pad, bottom_pad = 66 + len(unit) * 11, 20, 24, 44

    # 使用者明確要求：不管點數多少，圖表一律用固定像素寬（每個點留夠讓日期標籤
    # 不會互相重疊的間距，不是把點擠更近），外層永遠包一層 overflow-x:auto 的
    # 捲動容器——點數不夠多、寬度小於容器可視範圍時瀏覽器本來就不會生出捲軸
    # （這是正常行為，不是沒做），但只要點數一多到超出可視範圍，捲軸機制保證
    # 都在，不用另外判斷「需不需要」。
    # 110px 是抓「YYYY-MM-DD」這種 10 字元日期標籤在 font-size 10 底下的寬度
    # 再留一點間隙，兩個點的間距只要不小於這個數字，相鄰的日期標籤就不會重疊。
    min_point_spacing = 110
    width = max(width, left_pad + right_pad + max(len(points) - 1, 0) * min_point_spacing)

    plot_w = width - left_pad - right_pad
    plot_h = height - top_pad - bottom_pad

    values = [p["value"] for p in points]
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    n = len(points)
    step_x = plot_w / max(n - 1, 1)

    def x_at(i: int) -> float:
        return left_pad + i * step_x

    def y_at(v: float) -> float:
        return top_pad + plot_h - (v - lo) / span * plot_h

    coords = [(x_at(i), y_at(p["value"])) for i, p in enumerate(points)]

    grid = []
    ticks = 4
    for t in range(ticks + 1):
        val = lo + span * t / ticks
        y = y_at(val)
        grid.append(
            f'<line x1="{left_pad}" y1="{y:.1f}" x2="{width - right_pad}" y2="{y:.1f}" '
            f'class="grid-line" />'
            f'<text x="{left_pad - 8:.1f}" y="{y + 4:.1f}" font-size="11" class="axis-label" '
            f'text-anchor="end">{val:.1f}{unit}</text>'
        )

    # 置中對齊（text-anchor="middle"）的文字，中心點卡在最左/最右邊界上時，
    # 會有將近一半的文字寬度超出 SVG 的 viewBox 範圍被裁掉——第一筆跟最後一筆
    # 改成貼齊邊界內側對齊（start／end），不再置中，才不會被裁到看不全。
    # min_point_spacing 本身已經保證點與點之間留夠標籤寬度，所以每個點都可以
    # 直接標日期，不用再像以前那樣每隔幾個點才標一次（那是為了省空間，現在
    # 反而讓標籤看起來稀疏又跳號，不需要了——寬度不夠看就交給橫向捲軸處理）。
    x_labels = []
    for i, p in enumerate(points):
        if i == 0:
            anchor = "start"
        elif i == n - 1:
            anchor = "end"
        else:
            anchor = "middle"
        x_labels.append(
            f'<text x="{x_at(i):.1f}" y="{height - bottom_pad + 18:.1f}" font-size="10" '
            f'class="axis-label" text-anchor="{anchor}">{html.escape(p["x_label"])}</text>'
        )

    # baseline 參考線：這幾個指標的數列本身就是「跟 baseline 比的差距」，baseline
    # 永遠對應數值 0（第一筆點本身就是 0），畫一條橫線讓其他點跟 baseline 的落差
    # 一眼就看得出來，不用每次都對照最左邊那個點。
    baseline_line = ""
    if baseline_relative and lo <= 0 <= hi:
        by = y_at(0)
        baseline_line = (
            f'<line x1="{left_pad}" y1="{by:.1f}" x2="{width - right_pad}" y2="{by:.1f}" '
            f'stroke="#8fa3b8" stroke-width="1.5" stroke-dasharray="4,4" />'
            f'<text x="{width - right_pad - 4:.1f}" y="{by - 6:.1f}" font-size="10" '
            f'class="axis-label" text-anchor="end">baseline</text>'
        )

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    baseline_y = top_pad + plot_h
    area_path = (
        f"M{coords[0][0]:.1f},{baseline_y:.1f} "
        + " ".join(f"L{x:.1f},{y:.1f}" for x, y in coords)
        + f" L{coords[-1][0]:.1f},{baseline_y:.1f} Z"
    )

    point_svgs = []
    for i, ((x, y), p) in enumerate(zip(coords, points)):
        if i == 0:
            color = "#8fa3b8"
            label = "baseline"
        else:
            prev_val = points[i - 1]["value"]
            diff = p["value"] - prev_val
            improved = diff > 0 if higher_is_better else diff < 0
            color = "#4caf50" if improved else ("#ef5350" if diff != 0 else "#8fa3b8")
            if baseline_relative:
                # 這幾個指標的數列本身已經是「跟 baseline 比的差距」，第一筆
                # 恆為 0，再對這個數列算一次「相對前一點的比例」一樣會除以
                # 接近 0 的分母、炸出離譜數字（跟之前修過的百分點問題同一類）。
                # 直接顯示兩點間的差距（跟數列本身同單位），不要再換算成比例。
                label = f"{diff:+.2f}{unit}"
            else:
                pct = 0.0 if prev_val == 0 else diff / abs(prev_val) * 100
                label = f"{pct:+.1f}%"
        tooltip_text = (
            f"第 {i + 1} 筆（{p['commit']}）：{p['value']:.4f}{unit}（{label}）"
            f"{p.get('extra_note', '')}\n{p['description']}"
        )
        tooltip_attr = html.escape(tooltip_text).replace("\n", "&#10;")
        point_svgs.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" class="chart-point" '
            f'data-tip="{tooltip_attr}" '
            f'onmouseenter="showTip(event,this)" onmousemove="moveTip(event)" onmouseleave="hideTip()">'
            f"</circle>"
        )

    y_axis_title = (
        f'<text x="16" y="{top_pad + plot_h / 2:.1f}" font-size="11" class="axis-label" '
        f'text-anchor="middle" transform="rotate(-90 16 {top_pad + plot_h / 2:.1f})">數值（{unit}）</text>'
    )
    x_axis_title = (
        f'<text x="{left_pad + plot_w / 2:.1f}" y="{height - 4}" font-size="11" class="axis-label" '
        f'text-anchor="middle">實驗次序（保留下來的嘗試，依時間先後）</text>'
    )

    # SVG 一律用固定像素寬（不是 100%），外層 .chart-scroll 才有東西可以捲——
    # 點數不夠多、寬度小於容器可視範圍時瀏覽器自然不會顯示捲軸，但捲動機制
    # 本身一律都在，不看點數多寡切換模式。
    svg = (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'style="display:block;min-width:{width}px">'
        f"<defs><linearGradient id=\"{grad_id}\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">"
        f'<stop offset="0%" stop-color="{accent_color}" stop-opacity="0.35" />'
        f'<stop offset="100%" stop-color="{accent_color}" stop-opacity="0.02" />'
        f"</linearGradient></defs>"
        f"{''.join(grid)}"
        f"{baseline_line}"
        f'<path d="{area_path}" fill="url(#{grad_id})" stroke="none" />'
        f'<polyline fill="none" stroke="{accent_color}" stroke-width="3" stroke-linecap="round" '
        f'stroke-linejoin="round" points="{polyline}" />'
        f"{''.join(point_svgs)}"
        f"{''.join(x_labels)}"
        f"{y_axis_title}{x_axis_title}"
        f"</svg>"
    )
    return f'<div class="chart-scroll">{svg}</div>'


_STATUS_DOT_COLOR = {"keep": "#2e9e4a", "discard": "#d99a2b", "crash": "#e05353"}
# program.md「實驗規則」：hard gate 沒過時 cost_time 會被灌成「1000 起跳」的懲罰值，
# 不是真實秒數，正規化總覽圖遇到這種列要當成無效速度資料處理（跟 crash 同一類）。
_HARD_GATE_PENALTY_FLOOR = 1000.0


def _progress_overview_svg(
    all_rows: list[dict],
    reference_device_baseline: Optional[float],
    rebaseline_lookup: dict[str, dict],
    width: int = 900,
    height: int = 380,
) -> str:
    """研究進度總覽：仿照 Karpathy 原始 autoresearch 專案的 `progress.png`
    （x 軸是實驗次序、y 軸是正規化後的速度提升度，discard/crash 畫成背景灰點，
    keep 畫成綠點，另外疊一條只在「刷新目前最佳」時才往上走的階梯線）。跟現有
    的「速度提升度」大圖表是兩張不同用途的圖：那張只畫 kept、x 軸是日期，方便
    看單一指標隨時間的走勢；這張畫全部嘗試（含失敗的），x 軸是實驗次序，方便
    一眼看出「總共試了幾次、進步集中發生在哪幾次」的整體樣貌。2026-08-31 起
    拿掉了直接寫在圖上的改動說明文字（實驗一多會互相重疊看不清楚），改成只靠
    滑鼠移過去的 tooltip 看完整說明，圖上保持乾淨，另外把 x 軸標上實際的輪數
    刻度方便對照。"""
    experiments = [r for r in all_rows if r.get("status") in ("keep", "discard", "crash")]
    total = len(experiments)
    kept_n = sum(1 for r in experiments if r.get("status") == "keep")
    title = f"研究進度總覽：共 {total} 次實驗、{kept_n} 次留下改進"

    if not experiments:
        return f'<div class="section-title">{html.escape(title)}</div><p class="empty">（還沒有資料）</p>'

    scatter: list[dict] = []  # 畫得出速度數字的點（keep/discard，crash 另外處理）
    kept_points: list[dict] = []  # 只有 keep，用來畫「目前最佳」階梯線
    crash_points: list[dict] = []  # crash 沒有有效的 cost_time，不能套正規化公式
    invalid_points: list[dict] = []  # hard gate 沒過的 discard：cost_time 是懲罰值，不是真實速度
    for i, r in enumerate(experiments):
        status = r.get("status") or ""
        base_info = {
            "x": i + 1,
            "status": status,
            "commit": r.get("commit") or "",
            "description": r.get("description") or "",
            "ts": r.get("_ts"),
        }
        if status == "crash":
            # crash 代表這次執行直接壞掉，根本沒測到有效速度——`cost_time`
            # 依規則是填 0（或某些舊資料是別的無意義佔位值），絕對不能拿去套
            # 正規化公式當成「這次跑了幾秒」，那樣算出來的百分比毫無意義、
            # 還會把整張圖的 y 軸尺度炸壞（例如當成 0 秒會變成離譜的正提升，
            # 當成佔位的大數字又會變成離譜的負提升）。只記 x 位置，不算 y 值。
            crash_points.append(base_info)
            continue
        raw_cost_time = r.get("_cost_time")
        if raw_cost_time is not None and raw_cost_time >= _HARD_GATE_PENALTY_FLOOR:
            # program.md「實驗規則」明訂：hard gate（quality_pass_rate／
            # content_coverage／chunk 大小／redundancy_ratio 任一項）沒過的話，
            # cost_time 會被灌成一個「1000 起跳」的懲罰值，用來保證這個嘗試在
            # 「誰切得快」的比較裡必輸——這個數字本來就不是真實秒數，套正規化
            # 公式會被放大成離譜的負值（實測發生過 -90143%），把整張圖的 y 軸
            # 尺度拉爆。跟 crash 用同一種處理方式：只記 x 位置，不算 y 值。
            invalid_points.append({**base_info, "raw_cost_time": raw_cost_time})
            continue
        pct = _normalized_pct_any(r, reference_device_baseline, rebaseline_lookup)
        if pct is None:
            continue
        point = {**base_info, "value": pct}
        scatter.append(point)
        if status == "keep":
            kept_points.append(point)

    if not scatter and not crash_points and not invalid_points:
        return (
            f'<div class="section-title">{html.escape(title)}</div>'
            '<p class="empty">（還沒有可正規化比較的資料——設備基準機制上線之後才會有）</p>'
        )

    # 找出「目前最佳」的刷新點：依時間序走一遍 kept_points，只要比目前看過的
    # 最大值還大，就是一次刷新，記下來畫階梯線跟文字標註；其餘 keep 點仍畫成
    # 綠點，但不進這條線、也不加文字，避免標籤多到互相重疊看不清楚。
    milestones: list[dict] = []
    running_best = float("-inf")
    for p in kept_points:
        if p["value"] > running_best:
            running_best = p["value"]
            milestones.append(p)

    # bottom_pad 留夠三層東西的空間（由上到下）：crash 三角形標記、x 軸「第幾輪」
    # 刻度文字、x 軸標題。milestone 不再往圖上寫斜體說明文字了（改成只靠 tooltip
    # 看，見下方 x_ticks 那段的註解），所以 top_pad／right_pad 不用再留給文字
    # 標籤的安全邊界，跟其他圖表一樣用一般的留白就好。
    left_pad, top_pad, right_pad, bottom_pad = 56, 26, 24, 68
    max_x = max(p["x"] for p in (scatter + crash_points + invalid_points))
    width = max(width, left_pad + right_pad + max_x * 9)
    plot_w = width - left_pad - right_pad
    plot_h = height - top_pad - bottom_pad

    values = [p["value"] for p in scatter]
    # scatter 可能整批都是空的（例如所有實驗剛好都 crash、還沒有任何 keep/discard
    # 測到有效速度）——這種情況沒有速度數字可以決定 y 軸範圍，退回一個中性的
    # 0%~1% 區間，圖表只會顯示底部那排 crash 標記，不會因為 min()/max() 對空
    # list 報錯而整頁掛掉。
    lo, hi = (min(0.0, min(values)), max(values)) if values else (0.0, 1.0)
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo

    def x_at(x: int) -> float:
        return left_pad + (x - 1) / max(max_x - 1, 1) * plot_w

    def y_at(v: float) -> float:
        return top_pad + plot_h - (v - lo) / span * plot_h

    grid = []
    ticks = 4
    for t in range(ticks + 1):
        val = lo + span * t / ticks
        y = y_at(val)
        grid.append(
            f'<line x1="{left_pad}" y1="{y:.1f}" x2="{width - right_pad}" y2="{y:.1f}" class="grid-line" />'
            f'<text x="{left_pad - 8:.1f}" y="{y + 4:.1f}" font-size="11" class="axis-label" '
            f'text-anchor="end">{val:.0f}%</text>'
        )

    # 階梯線：從第一個 milestone（第一筆 keep，通常就是 baseline 自己）開始，
    # 每次刷新最佳就先畫一段水平線接到新的 x，再垂直跳到新的 y——只在真的
    # 刷新時才畫線段，中間那些沒刷新的 keep 點不連進這條線。
    step_segments = []
    if milestones:
        prev_y = y_at(milestones[0]["value"])
        path = f"M{x_at(milestones[0]['x']):.1f},{prev_y:.1f}"
        for m in milestones[1:]:
            nx, ny = x_at(m["x"]), y_at(m["value"])
            path += f" L{nx:.1f},{prev_y:.1f} L{nx:.1f},{ny:.1f}"
            prev_y = ny
        # 延伸到最後一個實驗的 x，代表「目前最佳」一直保持到現在
        path += f" L{x_at(max_x):.1f},{prev_y:.1f}"
        step_segments.append(
            f'<path d="{path}" fill="none" stroke="#2e9e4a" stroke-width="2.5" '
            f'stroke-linecap="round" stroke-linejoin="round" />'
        )

    # crash 標記：畫在圖表最底部的固定高度（不對應任何速度數字，crash 本來就
    # 沒有有效的 cost_time 可畫），用一個小三角形跟其他圓點區分開，純粹標示
    # 「這個實驗次序上發生過 crash」，只靠 x 位置提供資訊。
    crash_y = top_pad + plot_h + 6
    dots = []
    for p in crash_points:
        x = x_at(p["x"])
        tooltip = html.escape(f"第 {p['x']} 次實驗（{p['commit']}，crash）\n{p['description']}").replace(
            "\n", "&#10;"
        )
        dots.append(
            f'<path d="M{x:.1f},{crash_y - 4:.1f} L{x - 4:.1f},{crash_y + 4:.1f} L{x + 4:.1f},{crash_y + 4:.1f} Z" '
            f'fill="{_STATUS_DOT_COLOR["crash"]}" opacity="0.75" class="chart-point" data-tip="{tooltip}" '
            f'onmouseenter="showTip(event,this)" onmousemove="moveTip(event)" onmouseleave="hideTip()"></path>'
        )

    # hard gate 沒過的無效點：跟 crash 畫在同一條底部基準線上，用菱形跟 crash
    # 的三角形、正常點的圓形區分開，顏色沿用該列自己的 status（目前只會是
    # discard，因為 keep 規則上不可能 hard_gate_passed=False）。
    for p in invalid_points:
        x = x_at(p["x"])
        tooltip = html.escape(
            f"第 {p['x']} 次實驗（{p['commit']}，{p['status']}，hard gate 未通過）：\n"
            f"cost_time={p['raw_cost_time']:.2f}（懲罰值，非真實速度，不計入圖表數值）\n{p['description']}"
        ).replace("\n", "&#10;")
        dots.append(
            f'<path d="M{x:.1f},{crash_y - 5:.1f} L{x + 5:.1f},{crash_y:.1f} L{x:.1f},{crash_y + 5:.1f} '
            f'L{x - 5:.1f},{crash_y:.1f} Z" fill="{_STATUS_DOT_COLOR.get(p["status"], "#8fa3b8")}" '
            f'opacity="0.75" class="chart-point" data-tip="{tooltip}" '
            f'onmouseenter="showTip(event,this)" onmousemove="moveTip(event)" onmouseleave="hideTip()"></path>'
        )

    # 背景灰點（discard）先畫，keep 的綠點後畫，milestone 再疊最上層，
    # 確保重要的點不會被一般的點蓋住。
    for p in scatter:
        if p["status"] == "keep":
            continue
        x, y = x_at(p["x"]), y_at(p["value"])
        tooltip = html.escape(
            f"第 {p['x']} 次實驗（{p['commit']}，{p['status']}）：{p['value']:.2f}%\n{p['description']}"
        ).replace("\n", "&#10;")
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{_STATUS_DOT_COLOR.get(p["status"], "#8fa3b8")}" '
            f'opacity="0.55" class="chart-point" data-tip="{tooltip}" '
            f'onmouseenter="showTip(event,this)" onmousemove="moveTip(event)" onmouseleave="hideTip()"></circle>'
        )
    milestone_xs = {m["x"] for m in milestones}
    for p in kept_points:
        x, y = x_at(p["x"]), y_at(p["value"])
        is_milestone = p["x"] in milestone_xs
        r_px = 6.5 if is_milestone else 4.5
        stroke = ' stroke="#ffffff" stroke-width="1.5"' if is_milestone else ""
        tooltip = html.escape(
            f"第 {p['x']} 次實驗（{p['commit']}，keep{'，目前最佳' if is_milestone else ''}）："
            f"{p['value']:.2f}%\n{p['description']}"
        ).replace("\n", "&#10;")
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r_px}" fill="{_STATUS_DOT_COLOR["keep"]}"{stroke} '
            f'class="chart-point" data-tip="{tooltip}" '
            f'onmouseenter="showTip(event,this)" onmousemove="moveTip(event)" onmouseleave="hideTip()"></circle>'
        )

    # 2026-08-31 起拿掉了原本沿對角線寫在圖上的改動說明文字——實驗次數一多，
    # 這些斜寫的文字會互相疊在一起看不清楚（使用者截圖回報過這個問題），改成
    # 只靠滑鼠移過去的 tooltip 看完整說明，圖上維持乾淨。取而代之的是把 x 軸
    # 標上「第幾輪」的刻度，方便對照。

    # x 軸刻度：實驗次數少的時候每輪都標，次數一多改成每 5／10／20 輪標一次，
    # 不然刻度文字會互相擠在一起重疊。一律確保第 1 輪跟最後一輪都有標籤，
    # 讓人看得出總範圍，不是只看得到中間那幾個整數倍數的刻度。
    if max_x <= 20:
        step = 1
    elif max_x <= 60:
        step = 5
    elif max_x <= 150:
        step = 10
    else:
        step = 20
    tick_xs = sorted({1, *range(step, max_x + 1, step), max_x})
    x_ticks = []
    for tx in tick_xs:
        anchor = "start" if tx == 1 else ("end" if tx == max_x else "middle")
        x_ticks.append(
            f'<text x="{x_at(tx):.1f}" y="{top_pad + plot_h + 24:.1f}" font-size="10" '
            f'class="axis-label" text-anchor="{anchor}">{tx}</text>'
        )

    x_axis_title = (
        f'<text x="{left_pad + plot_w / 2:.1f}" y="{height - 6}" font-size="11" class="axis-label" '
        f'text-anchor="middle">實驗次序（第幾輪，含失敗的）</text>'
    )
    y_axis_title = (
        f'<text x="14" y="{top_pad + plot_h / 2:.1f}" font-size="11" class="axis-label" text-anchor="middle" '
        f'transform="rotate(-90 14 {top_pad + plot_h / 2:.1f})">正規化後速度提升度（%，越高越好）</text>'
    )

    svg = (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'style="display:block;min-width:{width}px">'
        f"{''.join(grid)}"
        f"{''.join(step_segments)}"
        f"{''.join(dots)}"
        f"{''.join(x_ticks)}"
        f"{x_axis_title}{y_axis_title}"
        f"</svg>"
    )

    legend_items = [
        '<span><i style="background:#d99a2b"></i>discard</span>',
        '<span><i style="background:#e05353"></i>crash</span>',
        '<span><i style="background:#2e9e4a"></i>keep</span>',
        '<span><i style="background:#2e9e4a;width:14px;height:3px;border-radius:0"></i>目前最佳（階梯線）</span>',
    ]
    if invalid_points:
        legend_items.append(
            '<span><i style="background:#d99a2b;border-radius:2px;transform:rotate(45deg)"></i>'
            "hard gate 未過（懲罰值，不計入數值）</span>"
        )
    legend = '<div class="overview-legend">' + "".join(legend_items) + "</div>"

    return (
        f'<div class="section-title">{html.escape(title)}</div>'
        f'<div class="subtitle" style="margin-bottom:0.5rem">灰／橘／紅點是每一次嘗試（keep/discard/crash 都畫出來），'
        "綠色階梯線只在刷新目前最佳紀錄時才往上走；把滑鼠移到點上可以看到那次改動的完整說明。</div>"
        f"{legend}"
        f'<div class="chart-scroll">{svg}</div>'
    )


def _growth_pct(first: float, last: float, higher_is_better: bool) -> float:
    if first == 0:
        return 0.0
    raw = (last - first) / abs(first) * 100
    return raw if higher_is_better else -raw


# ---------------------------------------------------------------------------
# 頁面組裝
# ---------------------------------------------------------------------------


def _sidebar_html(active_page: str) -> str:
    items = []
    for key, label, enabled in SIDEBAR_ITEMS:
        classes = "nav-item"
        if key == active_page:
            classes += " active"
        if not enabled:
            classes += " disabled"
            items.append(
                f'<div class="{classes}" title="待開發">{html.escape(label)} '
                f'<span class="badge-soon">待開發</span></div>'
            )
        else:
            items.append(f'<a class="{classes}" href="/">{html.escape(label)}</a>')
    return "".join(items)


def _minicard_html(
    key: str,
    cfg: dict,
    series: list[Optional[float]],
    selected: bool,
    qs_keep: str,
    extra_line: str = "",
    card_value_override: Optional[str] = None,
    growth_override: Optional[tuple[str, str]] = None,
    warning_line: str = "",
) -> str:
    clean = [v for v in series if v is not None]
    if len(clean) >= 2:
        if cfg.get("baseline_relative"):
            # 這幾個指標的原始數列本來就是「跟 baseline 比的累積進步 %」，
            # 大數字（metric-card-value）已經在顯示這個累積值了——小字徽章
            # 改成「這一筆比上一筆 keep 又進步了幾個百分點」，用直接相減
            # 就好（數列本身是可加減的百分點差距，不是比例，不會有除以 0
            # 的問題），這樣兩個數字才各自有意義，不會重複顯示同一個東西。
            growth = clean[-1] - clean[-2]
        else:
            growth = _growth_pct(clean[0], clean[-1], cfg["higher_is_better"])
        growth_str = f"{growth:+.1f}{cfg['unit']}"
        growth_class = "up" if growth >= 0 else "down"
        latest = f"{clean[-1]:.2f}{cfg['unit']}"
    elif len(clean) == 1:
        growth_str = "（還沒有足夠資料比較）"
        growth_class = ""
        latest = f"{clean[0]:.2f}{cfg['unit']}"
    else:
        growth_str = "（尚無資料）"
        growth_class = ""
        latest = "—"

    # 速度提升度卡片：小字徽章改成「比目前為止最好的那一筆 keep 進步了幾個
    # 百分點」並附上那筆最佳紀錄的日期，而不是只跟上一筆比——上一筆不一定是
    # 目前最好的（中間可能有一段時間曲線下降過），跟「目前最好」比才能看出
    # 這次改動有沒有真的創新高。
    if growth_override is not None:
        growth_str, growth_class = growth_override

    # content_coverage 這類指標的「大數字」改顯示原始絕對值（例如 99.96%），
    # 不是跟 baseline 的差值——差值本身在還沒有任何嘗試動到內容邏輯時永遠是
    # 0，看起來像沒資料，改成絕對值後 100% 才能直觀對應「完全沒丟東西」，
    # 跟「組裝正確率」卡片的呈現方式一致。折線圖／成長徽章的趨勢邏輯不受影響，
    # 只有這個 headline 數字換算法。
    if card_value_override is not None:
        latest = card_value_override

    spark = _mini_sparkline_svg(clean, cfg["color"])
    active_class = " active" if selected else ""
    href = f"/?metric={key}{qs_keep}"
    extra_line_html = (
        f'<div class="metric-card-extra">{html.escape(extra_line)}</div>' if extra_line else ""
    )
    # 「設備基準異常」警示：這次 session 一開始量到的設備基準跟參考值差太多
    # （見 program.md「開始每次執行前：設備基準測量」），只在 cost_time 卡片
    # 顯示，因為只有這個指標的計算會被設備基準污染。
    warning_line_html = (
        f'<div class="metric-card-warning">⚠️ {html.escape(warning_line)}</div>' if warning_line else ""
    )
    return (
        f'<a class="metric-card{active_class}" href="{html.escape(href)}">'
        f'<div class="metric-card-label">{html.escape(cfg["label"])}</div>'
        f'<div class="metric-card-value">{latest}</div>'
        f'<div class="metric-card-growth {growth_class}">{growth_str}</div>'
        f"{extra_line_html}"
        f"{warning_line_html}"
        f'<div class="metric-card-spark">{spark}</div>'
        f"</a>"
    )


def render_page(query: dict) -> str:
    selected_metric = query.get("metric", ["cost_time"])[0]
    if selected_metric not in METRICS:
        selected_metric = "cost_time"
    start = query.get("start", [""])[0]
    end = query.get("end", [""])[0]

    all_rows = _enrich(_read_rows())
    # baseline 一律從「完整、未經日期篩選」的歷史裡算，日期篩選只決定要顯示
    # 哪些點——不然只要篩選範圍剛好排除掉第一筆實驗，篩選後的第一筆就會被誤當
    # 成新的 baseline，同一筆實驗在篩選前後會顯示不同的進步幅度（codex 審查
    # 抓到的問題）。
    all_kept = [r for r in all_rows if r.get("status") == "keep"]
    rows = _filter_by_date(all_rows, start, end)
    kept = [r for r in rows if r.get("status") == "keep"]

    qs_keep = ""
    if start:
        qs_keep += f"&start={html.escape(start)}"
    if end:
        qs_keep += f"&end={html.escape(end)}"

    # 每個指標的 baseline 都從 all_kept 算一次，series 才用（可能被日期篩選過的）
    # kept 算要顯示的點。cost_time 不在這個通用迴圈裡算（見 METRICS 字典裡的
    # 註解）——它需要 rebaseline 查詢表跟參考設備基準，這兩個都得從「完整、未經
    # 日期篩選」的 all_rows 算，理由跟其他指標的 baseline 一樣：篩選範圍不能
    # 影響參考值本身。
    metric_series: dict[str, list[Optional[float]]] = {}
    for key, cfg in METRICS.items():
        if key == "cost_time":
            continue
        baseline = cfg["baseline_fn"](all_kept)
        metric_series[key] = cfg["series_fn"](kept, baseline)

    reference_device_baseline = _reference_device_baseline(all_rows)
    rebaseline_lookup = _rebaseline_lookup(all_rows)
    metric_series["cost_time"] = _speed_improvement_series(kept, reference_device_baseline, rebaseline_lookup)

    # 「研究進度總覽」用完整、未經日期篩選的歷史（跟 baseline／參考設備基準
    # 一樣的理由：這張圖的用意是呈現整個專案至今的全貌，不應該被目前選的日期
    # 範圍過濾掉一部分，不然「共 N 次實驗」這個標題數字會跟著篩選條件變動、
    # 失去「總覽」的意義）。
    progress_overview_html = _progress_overview_svg(all_rows, reference_device_baseline, rebaseline_lookup)

    # --- 設備基準異常警示：取「完整、未經日期篩選」歷史裡最新一筆的異常註記，
    # 不受目前的日期篩選影響，反映的是「現在」的設備狀態，不是篩選範圍內的
    # 某個舊狀態。---
    device_anomaly_warning = all_rows[-1].get("_device_anomaly_note") if all_rows else ""

    # --- 距離通知門檻 ---
    speed_clean = [v for v in metric_series["cost_time"] if v is not None]
    best_speed_pct = max(speed_clean) if speed_clean else 0.0
    threshold_progress = max(0.0, min(100.0, best_speed_pct / NOTIFY_THRESHOLD_PCT * 100))
    threshold_note = (
        f"已達 {NOTIFY_THRESHOLD_PCT:.0f}% 通知門檻，等待新紀錄"
        if best_speed_pct >= NOTIFY_THRESHOLD_PCT
        else f"距離 {NOTIFY_THRESHOLD_PCT:.0f}% 通知門檻還差 {NOTIFY_THRESHOLD_PCT - best_speed_pct:.1f} 個百分點"
    )

    # --- 四個小卡片 ---
    # cost_time 卡片額外加一行「最新一筆同時段比較」——主要的累積 % 徽章跟
    # 最原始 baseline 比，機器負載波動時容易失真；這行是排除負載干擾、只看
    # 最新這一筆改動的準確數字，兩個一起顯示，不用只靠 tooltip 才看得到。
    latest_same_session_pct = _same_session_pct(kept[-1]) if kept else None
    cost_time_extra = (
        f"最新一筆同時段比較：{latest_same_session_pct:+.1f}%"
        if latest_same_session_pct is not None
        else ""
    )
    # content_coverage 的「大數字」改顯示原始絕對保留率（例如 99.96%），不是
    # 跟 baseline 的差值，理由見 _minicard_html 裡的註解。
    latest_coverage = kept[-1].get("_content_coverage") if kept else None
    content_coverage_card_value = (
        f"{latest_coverage * 100:.2f}%" if latest_coverage is not None else None
    )
    # 速度提升度的小字徽章改成「跟目前為止最好的那一筆 keep 比」，理由見
    # `_speed_growth_vs_best` 的註解。
    speed_growth_override = _speed_growth_vs_best(kept, metric_series["cost_time"])
    minicards = "".join(
        _minicard_html(
            key,
            cfg,
            metric_series[key],
            key == selected_metric,
            qs_keep,
            extra_line=cost_time_extra if key == "cost_time" else "",
            card_value_override=content_coverage_card_value if key == "content_coverage" else None,
            growth_override=speed_growth_override if key == "cost_time" else None,
            warning_line=device_anomaly_warning if key == "cost_time" else "",
        )
        for key, cfg in METRICS.items()
    )

    # --- 中間大圖 ---
    cfg = METRICS[selected_metric]
    big_label, big_unit, big_higher, big_color = cfg["label"], cfg["unit"], cfg["higher_is_better"], cfg["color"]
    series = metric_series[selected_metric]

    big_points = [
        {
            "value": v,
            "x_label": _fmt_date(r["_ts"])[:10] if r["_ts"] else f"#{i + 1}",
            "commit": r.get("commit") or "",
            "description": r.get("description") or "",
            "extra_note": _same_session_note(r) if selected_metric == "cost_time" else "",
        }
        for i, (r, v) in enumerate(zip(kept, series))
        if v is not None
    ]
    big_chart = _big_chart_svg(
        big_points,
        big_higher,
        big_unit,
        big_color,
        f"chartGrad_{selected_metric}",
        baseline_relative=cfg.get("baseline_relative", False),
    )
    big_desc = cfg["desc"]

    # --- 右側統計 ---
    # 「總實驗數」只算真正的實驗嘗試（keep/discard/crash），rebaseline 是
    # 2026-08-14 設備基準機制的一次性回測資料，不是新的實驗嘗試，不計入。
    total = sum(1 for r in rows if r.get("status") in ("keep", "discard", "crash"))
    kept_n = len(kept)
    discarded_n = sum(1 for r in rows if r.get("status") == "discard")
    crashed_n = sum(1 for r in rows if r.get("status") == "crash")
    max_count = max(kept_n, discarded_n, crashed_n, 1)

    def _bar(label: str, count: int, color: str) -> str:
        pct = count / max_count * 100
        return (
            f'<div class="stat-bar-row"><span class="stat-bar-label">{label}</span>'
            f'<div class="stat-bar-track"><div class="stat-bar-fill" style="width:{pct:.0f}%;background:{color}"></div></div>'
            f'<span class="stat-bar-count">{count}</span></div>'
        )

    stats_bars = (
        _bar("keep", kept_n, "#4caf50") + _bar("discard", discarded_n, "#ffa726") + _bar("crash", crashed_n, "#ef5350")
    )

    latest_seconds = kept[-1]["_seconds"] if kept and kept[-1]["_seconds"] is not None else None
    latest_seconds_str = f"{latest_seconds:.3f} 秒" if latest_seconds is not None else "—"

    # --- 歷史紀錄表 ---
    # `csv.DictReader` 遇到「欄位數比表頭少」的資料列時，缺的欄位值是 None
    # （restval 預設值），不是空字串——`r.get(key, "")` 只有在 key 不存在時才會
    # 用到 default，key 存在但值是 None 時還是回傳 None，直接丟給 html.escape()
    # 會炸掉。這裡一律用 `or ""` 把 None 轉成空字串，不管是 key 不存在還是值是
    # None 都能安全處理，短列資料不會讓整頁掛掉。
    history_rows = "".join(
        f'<tr><td>{html.escape(_fmt_date(r["_ts"]))}</td>'
        f'<td>{html.escape(r.get("commit") or "")}</td>'
        f'<td class="status-{html.escape(r.get("status") or "")}">{html.escape(r.get("status") or "")}</td>'
        f'<td>{html.escape(r.get("cost_time") or "")}</td>'
        f'<td>{html.escape(r.get("description") or "")}</td></tr>'
        for r in reversed(rows[-HISTORY_ROW_LIMIT:])
    )
    history_count = min(len(rows), HISTORY_ROW_LIMIT)

    date_label = f"{start or '最早'} ~ {end or '最新'}" if (start or end) else "全部日期"

    return f"""<!doctype html>
<html lang="zh-Hant" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>chunking-autoresearch 儀表板</title>
<style>
:root {{
  --bg: #eef1fb; --panel: #ffffff; --sidebar: #3366ff; --sidebar-text: #d6e0ff;
  --text: #16213a; --muted: #7a88a8; --accent: #3366ff; --chart-area: rgba(51,102,255,0.14);
  --grid: #eef1fb; --border: #eaedf7;
}}
html[data-theme="dark"] {{
  --bg: #0b0f14; --panel: #121a24; --sidebar: #0e1520; --sidebar-text: #a9b8cf;
  --text: #e6edf3; --muted: #8695ab; --accent: #63a4ff; --chart-area: rgba(99,164,255,0.14);
  --grid: #223142; --border: #223142;
}}
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui, -apple-system, "Noto Sans TC", sans-serif; margin: 0; background: var(--bg); color: var(--text); }}
.layout {{ display: flex; min-height: 100vh; }}
.sidebar {{ width: 200px; flex-shrink: 0; background: var(--sidebar); color: var(--sidebar-text); padding: 1.25rem 1rem; }}
.sidebar-brand {{ font-weight: 700; font-size: 1.1rem; color: #fff; margin-bottom: 1.5rem; }}
.nav-item {{ display: block; padding: 0.55rem 0.75rem; border-radius: 8px; color: var(--sidebar-text); text-decoration: none; margin-bottom: 0.25rem; font-size: 0.9rem; }}
.nav-item.active {{ background: rgba(255,255,255,0.18); color: #fff; font-weight: 600; }}
.nav-item.disabled {{ opacity: 0.5; display: flex; justify-content: space-between; align-items: center; cursor: default; }}
.badge-soon {{ font-size: 0.65rem; background: rgba(255,255,255,0.15); padding: 0.1rem 0.4rem; border-radius: 6px; }}
.main {{ flex: 1; min-width: 0; }}
.topbar {{ display: flex; align-items: center; gap: 1rem; padding: 0.9rem 1.5rem; background: var(--panel); border-bottom: 1px solid var(--border); flex-wrap: wrap; }}
.topbar input[type="text"], .topbar input[type="date"] {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.7rem; color: var(--text); font-size: 0.85rem; }}
.search-box {{ flex: 1; min-width: 160px; }}
.date-form {{ display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; color: var(--muted); }}
.theme-toggle, .bell {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.6rem; cursor: pointer; font-size: 0.9rem; color: var(--text); position: relative; }}
.avatar {{ width: 32px; height: 32px; border-radius: 50%; background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 0.8rem; font-weight: 600; }}
.content {{ padding: 1.5rem; }}
h1 {{ font-size: 1.5rem; margin: 0 0 0.2rem; }}
.subtitle {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 1.25rem; }}
.panel {{ background: var(--panel); border-radius: 16px; padding: 1.1rem 1.25rem; border: 1px solid var(--border); box-shadow: 0 2px 10px rgba(30,50,100,0.06); }}
.chart-tooltip {{ position: fixed; display: none; background: #1f2937; color: #fff; padding: 0.5rem 0.7rem; border-radius: 8px; font-size: 0.78rem; white-space: pre-line; max-width: 280px; box-shadow: 0 4px 14px rgba(0,0,0,0.28); z-index: 999; pointer-events: none; line-height: 1.4; }}
.chart-point {{ cursor: pointer; }}
.history-scroll {{ max-height: 420px; overflow-y: scroll; margin-top: 0.5rem; }}
.chart-scroll {{ overflow-x: auto; padding-bottom: 12px; scrollbar-width: thin; }}
.chart-scroll::-webkit-scrollbar {{ height: 7px; }}
.chart-scroll::-webkit-scrollbar-track {{ background: transparent; }}
.chart-scroll::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
.history-scroll table {{ margin-top: 0; }}
.history-scroll thead th {{ position: sticky; top: 0; background: var(--panel); z-index: 1; }}
.grid-top {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1rem; }}
.metric-card {{ display: block; text-decoration: none; color: var(--text); }}
.metric-card-label {{ font-size: 0.8rem; color: var(--muted); }}
.metric-card-value {{ font-size: 1.5rem; font-weight: 700; margin: 0.15rem 0; }}
.metric-card-growth {{ font-size: 0.8rem; }}
.metric-card-growth.up {{ color: #2e9e4a; }}
.metric-card-growth.down {{ color: #e05353; }}
.metric-card-extra {{ font-size: 0.72rem; color: var(--muted); margin-top: 0.15rem; }}
.metric-card-warning {{ font-size: 0.72rem; color: #d99a2b; margin-top: 0.15rem; font-weight: 600; }}
.metric-card.active {{ outline: 2px solid var(--accent); }}
.composite-panel {{ display: flex; justify-content: space-between; align-items: center; gap: 1.5rem; margin-bottom: 1rem; flex-wrap: wrap; }}
.composite-score {{ font-size: 2.2rem; font-weight: 800; color: var(--accent); }}
.progress-track {{ background: var(--grid); border-radius: 6px; height: 10px; width: 260px; overflow: hidden; }}
.progress-fill {{ background: var(--accent); height: 100%; }}
.body-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 1rem; align-items: start; }}
.body-grid > .panel {{ min-width: 0; }}
.side-stack {{ display: flex; flex-direction: column; gap: 1rem; }}
.stat-bar-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; font-size: 0.8rem; }}
.stat-bar-label {{ width: 56px; color: var(--muted); }}
.stat-bar-track {{ flex: 1; background: var(--grid); border-radius: 6px; height: 8px; overflow: hidden; }}
.stat-bar-fill {{ height: 100%; }}
.stat-bar-count {{ width: 28px; text-align: right; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 0.75rem; }}
td, th {{ border-bottom: 1px solid var(--border); padding: 5px 8px; text-align: left; font-size: 0.8rem; }}
.status-keep {{ color: #2e9e4a; }}
.status-discard {{ color: #d99a2b; }}
.status-crash {{ color: #e05353; }}
.status-rebaseline {{ color: #6c8ebf; }}
.grid-line {{ stroke: var(--grid); stroke-width: 1; }}
.axis-label {{ fill: var(--muted); }}
.empty {{ color: var(--muted); font-size: 0.85rem; }}
.section-title {{ font-weight: 600; margin-bottom: 0.5rem; }}
.overview-legend {{ display: flex; gap: 1.1rem; flex-wrap: wrap; font-size: 0.78rem; color: var(--muted); margin-bottom: 0.5rem; }}
.overview-legend span {{ display: inline-flex; align-items: center; gap: 0.35rem; }}
.overview-legend i {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
.footnote {{ color: var(--muted); font-size: 0.75rem; margin-top: 1rem; }}
@media (max-width: 900px) {{
  .layout {{ flex-direction: column; }}
  .sidebar {{ width: 100%; display: flex; gap: 0.5rem; overflow-x: auto; }}
  .grid-top {{ grid-template-columns: repeat(2, 1fr); }}
  .body-grid {{ grid-template-columns: 1fr; }}
}}
</style>
<script>
(function() {{
  var saved = localStorage.getItem('dashboard-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
}})();
function toggleTheme() {{
  var cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  var next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('dashboard-theme', next);
}}
function filterHistory(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('#historyTable tbody tr').forEach(function(tr) {{
    tr.style.display = tr.textContent.toLowerCase().indexOf(q) === -1 ? 'none' : '';
  }});
}}
function showTip(evt, el) {{
  var tip = document.getElementById('chartTooltip');
  tip.textContent = el.getAttribute('data-tip');
  tip.style.display = 'block';
  moveTip(evt);
}}
function moveTip(evt) {{
  var tip = document.getElementById('chartTooltip');
  if (tip.style.display !== 'block') return;
  var x = evt.clientX + 14, y = evt.clientY + 14;
  var maxX = window.innerWidth - tip.offsetWidth - 10;
  var maxY = window.innerHeight - tip.offsetHeight - 10;
  tip.style.left = Math.max(4, Math.min(x, maxX)) + 'px';
  tip.style.top = Math.max(4, Math.min(y, maxY)) + 'px';
}}
function hideTip() {{
  document.getElementById('chartTooltip').style.display = 'none';
}}
document.addEventListener('DOMContentLoaded', function() {{
  // 資料點是舊到新由左到右排列，圖表捲動容器預設停在最左邊（最舊那筆），
  // 使用者一打開頁面反而看不到最新結果，要自己往右捲——載入時直接捲到底，
  // 預設就看得到最新一筆。
  document.querySelectorAll('.chart-scroll').forEach(function(el) {{
    el.scrollLeft = el.scrollWidth;
  }});
}});
</script>
</head>
<body onload="(function(){{var t=localStorage.getItem('dashboard-theme'); if(t) document.documentElement.setAttribute('data-theme', t);}})()">
<div id="chartTooltip" class="chart-tooltip"></div>
<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-brand">chunking-autoresearch</div>
    {_sidebar_html("dashboard")}
  </nav>
  <div class="main">
    <div class="topbar">
      <input class="search-box" type="text" placeholder="搜尋歷史紀錄（commit / 說明）" oninput="filterHistory(this.value)">
      <form class="date-form" method="get">
        <input type="hidden" name="metric" value="{html.escape(selected_metric)}">
        日期：<input type="date" name="start" value="{html.escape(start)}" onchange="this.form.submit()">
        ~ <input type="date" name="end" value="{html.escape(end)}" onchange="this.form.submit()">
        <a href="/?metric={html.escape(selected_metric)}">全部</a>
      </form>
      <button class="bell" title="{html.escape(threshold_note)}">🔔</button>
      <button class="theme-toggle" onclick="toggleTheme()">🌓</button>
      <div class="avatar">YY</div>
      <span style="font-size:0.85rem;color:var(--muted)">yoyu0326</span>
    </div>
    <div class="content">
      <h1>Dashboard</h1>
      <div class="subtitle">chunking-autoresearch 即時結果 · 目前顯示範圍：{html.escape(date_label)}</div>

      <div class="panel composite-panel">
        <div>
          <div class="metric-card-label">{html.escape(threshold_note)}</div>
          <div class="progress-track"><div class="progress-fill" style="width:{threshold_progress:.0f}%"></div></div>
        </div>
      </div>

      <div class="panel" style="margin-bottom:1rem">
        {progress_overview_html}
      </div>

      <div class="grid-top">{minicards}</div>

      <div class="body-grid">
        <div class="panel">
          <div class="section-title">{html.escape(big_label)}</div>
          <div class="subtitle" style="margin-bottom:0.75rem">{html.escape(big_desc)}</div>
          {big_chart}
        </div>
        <div class="side-stack">
          <div class="panel">
            <div class="section-title">實驗統計</div>
            {stats_bars}
            <div class="footnote">總實驗數：{total}</div>
          </div>
          <div class="panel">
            <div class="section-title">本次執行資源</div>
            <div class="footnote" style="font-size:0.85rem;color:var(--text)">最新一筆耗時：{latest_seconds_str}</div>
            <div class="footnote" style="font-size:0.85rem;color:var(--text)">花費：$0.00（Phase 1 完全本機執行，零 API 成本）</div>
          </div>
        </div>
      </div>

      <div class="panel" style="margin-top:1rem">
        <div class="section-title">歷史紀錄（顯示最近 {history_count} 筆，超過表格高度可滑動捲動）</div>
        <div class="history-scroll">
          <table id="historyTable">
            <thead><tr><th>日期</th><th>commit</th><th>status</th><th>cost_time</th><th>description</th></tr></thead>
            <tbody>{history_rows}</tbody>
          </table>
        </div>
      </div>
      <p class="footnote">按瀏覽器的重新整理即可看到最新結果。資料來源：results.tsv</p>
    </div>
  </div>
</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        query = parse_qs(parts.query)
        body = render_page(query).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # 不用把每次 request 都印到 console


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"Dashboard: http://{host}:{PORT}/  (Ctrl+C 結束)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
