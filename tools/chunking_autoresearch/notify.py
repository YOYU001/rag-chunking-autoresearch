"""tools/chunking_autoresearch/notify.py

檢查 results.tsv 裡目前最好的 keep 紀錄，相對 baseline 的 cost_time 進步幅度
有沒有跨過門檻（預設 20%），有的話透過 `hermes send` 推播 Telegram 通知。

設計成「只在有新的、跨過門檻的最佳結果時才通知」——用 .last_notified 記錄上次
通知過的 commit，避免同一個結果每次 cron 執行都重複推播。

2026-08-14 起改用「設備基準正規化」判斷進步幅度，不是直接拿原始 cost_time 相減
——原始秒數會被機器負載跨天波動污染（同一份程式碼，機器忙的時候測起來就是比較
慢，看起來像退步），跟 dashboard.py 的 `_speed_improvement_series` 用同一套算法，
理由見 dashboard.py 裡那個函式的說明。

前提：這個腳本要在已經設定好 Telegram gateway 的 Hermes 環境裡執行（同一個容器內），
`hermes send` 才找得到已儲存的 bot token / home channel。

用法：python notify.py（通常由 cron 排程在每輪 chunking-autoresearch 跑完後呼叫）
"""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path
from typing import Optional

RESULTS_TSV = Path(__file__).resolve().parent / "results.tsv"
LAST_NOTIFIED_FILE = Path(__file__).resolve().parent / ".last_notified"
IMPROVEMENT_THRESHOLD_PCT = 20.0


def _safe_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _read_rows() -> list[dict]:
    if not RESULTS_TSV.exists():
        return []
    with RESULTS_TSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _rebaseline_lookup(all_rows: list[dict]) -> dict[str, dict]:
    """把 `status == "rebaseline"` 的紀錄整理成 `commit -> {cost_time,
    device_baseline_cost_time}` 的查詢表，給沒有自己 `device_baseline_cost_time`
    的舊 `keep` 紀錄拿去對照——跟 dashboard.py 裡同名函式邏輯一致。"""
    lookup: dict[str, dict] = {}
    for r in all_rows:
        if r.get("status") != "rebaseline":
            continue
        commit = r.get("commit")
        cost_time = _safe_float(r.get("cost_time"))
        device_baseline = _safe_float(r.get("device_baseline_cost_time"))
        if commit and cost_time is not None and device_baseline is not None:
            lookup[commit] = {"cost_time": cost_time, "device_baseline_cost_time": device_baseline}
    return lookup


def _reference_device_baseline(all_rows: list[dict]) -> Optional[float]:
    return next(
        (v for r in all_rows if (v := _safe_float(r.get("device_baseline_cost_time"))) is not None),
        None,
    )


def _normalized_cost_time(
    r: dict, reference_device_baseline: Optional[float], rebaseline_lookup: dict[str, dict]
) -> Optional[float]:
    """把這一列的原始 cost_time 換算成「如果在參考設備狀態下會測到幾秒」，跟
    dashboard.py 的 `_speed_improvement_series` 用同一條公式。"""
    cost_time = _safe_float(r.get("cost_time"))
    device_baseline = _safe_float(r.get("device_baseline_cost_time"))
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
    return cost_time * reference_device_baseline / device_baseline


def _last_notified_commit() -> Optional[str]:
    if not LAST_NOTIFIED_FILE.exists():
        return None
    return LAST_NOTIFIED_FILE.read_text(encoding="utf-8").strip() or None


def main() -> None:
    all_rows = _read_rows()
    kept = [r for r in all_rows if r.get("status") == "keep"]
    if len(kept) < 2:
        return

    reference_device_baseline = _reference_device_baseline(all_rows)
    rebaseline_lookup = _rebaseline_lookup(all_rows)
    if reference_device_baseline is None:
        # 還沒有任何一筆 device_baseline_cost_time 資料（設備基準機制還沒
        # 開始跑過一次），沒有正規化的依據，先不判斷、不通知，等機制真正
        # 上線後再說。
        return

    scored = [
        (r, _normalized_cost_time(r, reference_device_baseline, rebaseline_lookup)) for r in kept
    ]
    scored = [(r, normalized) for r, normalized in scored if normalized is not None]
    if not scored:
        return

    best, best_normalized = min(scored, key=lambda pair: pair[1])
    pct = (reference_device_baseline - best_normalized) / reference_device_baseline * 100

    if pct < IMPROVEMENT_THRESHOLD_PCT:
        return
    if best["commit"] == _last_notified_commit():
        return

    raw_cost_time = _safe_float(best.get("cost_time"))
    raw_cost_time_str = f"{raw_cost_time:.6f}" if raw_cost_time is not None else best.get("cost_time", "")
    message = (
        f"[chunking-autoresearch] 找到 {pct:.1f}% 的進步（設備基準正規化後）\n"
        f"最佳結果 commit：{best['commit']}\n"
        f"cost_time：{raw_cost_time_str}（正規化後：{best_normalized:.6f}，"
        f"參考設備基準：{reference_device_baseline:.6f}）\n"
        f"說明：{best.get('description', '')}"
    )
    subprocess.run(["hermes", "send", "--to", "telegram", message], check=True)
    LAST_NOTIFIED_FILE.write_text(best["commit"], encoding="utf-8")


if __name__ == "__main__":
    main()
