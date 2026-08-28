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

# 一定要指到 main checkout 那份固定路徑，不能用「這支腳本自己在哪裡」去推算
# ——notify.py 是有進版控的檔案，Hermes 常常是在 worktree（例如
# .worktrees/chunking-autoresearch/）裡執行它，這時候 __file__ 指到的就會是
# worktree 自己那份幾乎沒資料的 results.tsv，不是 program.md 規定要讀寫的那份
# 固定路徑；跟 program.md「記錄結果」章節說明的、以及 2026-08-11 那次
# harness.py 相對路徑事故是同一類 bug。固定路徑不存在（例如在非 NAS 環境本機
# 測試）時才退回用檔案所在資料夾，方便開發測試。
_FIXED_RESULTS_TSV = Path(
    "/workspace/projects/AI-Benchmark-and-Evaluation-Platform/tools/chunking_autoresearch/results.tsv"
)
RESULTS_TSV = _FIXED_RESULTS_TSV if _FIXED_RESULTS_TSV.exists() else Path(__file__).resolve().parent / "results.tsv"
LAST_NOTIFIED_FILE = RESULTS_TSV.parent / ".last_notified"
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
    """一律印出目前的判斷結果到 stdout（給 Hermes 讀，不能只看 exit code 或
    有沒有發 Telegram——同一個跨過門檻的 commit 因為去重機制不會重複通知，
    但門檻狀態本身還是要能被觀察到）。"""
    all_rows = _read_rows()
    kept = [r for r in all_rows if r.get("status") == "keep"]
    if len(kept) < 2:
        print("THRESHOLD_STATUS: crossed=unknown reason=keep_rows_不足2筆")
        return

    reference_device_baseline = _reference_device_baseline(all_rows)
    rebaseline_lookup = _rebaseline_lookup(all_rows)
    if reference_device_baseline is None:
        # 還沒有任何一筆 device_baseline_cost_time 資料（設備基準機制還沒
        # 開始跑過一次），沒有正規化的依據，先不判斷、不通知，等機制真正
        # 上線後再說。
        print("THRESHOLD_STATUS: crossed=unknown reason=無_REFERENCE_DEVICE_BASELINE")
        return

    scored = [
        (r, _normalized_cost_time(r, reference_device_baseline, rebaseline_lookup)) for r in kept
    ]
    scored = [(r, normalized) for r, normalized in scored if normalized is not None]
    if not scored:
        print("THRESHOLD_STATUS: crossed=unknown reason=無_keep_列可換算正規化數字")
        return

    best, best_normalized = min(scored, key=lambda pair: pair[1])
    pct = (reference_device_baseline - best_normalized) / reference_device_baseline * 100
    crossed = pct >= IMPROVEMENT_THRESHOLD_PCT

    if not crossed:
        print(
            f"THRESHOLD_STATUS: crossed={crossed} pct={pct:.2f} "
            f"threshold={IMPROVEMENT_THRESHOLD_PCT:.0f} best_commit={best['commit']}"
        )
        return
    if best["commit"] == _last_notified_commit():
        print(
            f"THRESHOLD_STATUS: crossed={crossed} pct={pct:.2f} "
            f"threshold={IMPROVEMENT_THRESHOLD_PCT:.0f} best_commit={best['commit']} "
            f"reason=already_notified"
        )
        return

    print(
        f"THRESHOLD_STATUS: crossed={crossed} pct={pct:.2f} "
        f"threshold={IMPROVEMENT_THRESHOLD_PCT:.0f} best_commit={best['commit']} "
        f"reason=new_notification"
    )

    raw_cost_time = _safe_float(best.get("cost_time"))
    raw_cost_time_str = f"{raw_cost_time:.6f}" if raw_cost_time is not None else best.get("cost_time", "")
    message = (
        f"[chunking-autoresearch] 找到 {pct:.1f}% 的進步（設備基準正規化後）\n"
        f"最佳結果 commit：{best['commit']}\n"
        f"cost_time：{raw_cost_time_str}（正規化後：{best_normalized:.6f}，"
        f"參考設備基準：{reference_device_baseline:.6f}）\n"
        f"說明：{best.get('description', '')}"
    )
    # 上面的 THRESHOLD_STATUS 已經是這次執行唯一、正確的門檻判斷結果；下面發送
    # Telegram 失敗是另一件事（純遞送問題，不代表門檻判斷有變），刻意不讓它
    # 往外拋出、變成第二行 THRESHOLD_STATUS（那會被誤讀成「不知道有沒有跨過
    # 門檻」，跟已經印出的正確結果互相矛盾）——用不同的前綴記錄下來即可。
    try:
        subprocess.run(["hermes", "send", "--to", "telegram", message], check=True)
        LAST_NOTIFIED_FILE.write_text(best["commit"], encoding="utf-8")
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"NOTIFY_SEND_FAILED: reason={type(exc).__name__}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # main() 承諾一律印出 THRESHOLD_STATUS，讓 Hermes 不用檢查 exit code；
        # 未預期的例外（檔案壞掉、格式錯誤等）也要遵守這個承諾，印出來再往上
        # 拋，不能悄悄吞掉。
        print(f"THRESHOLD_STATUS: crossed=unknown reason=exception_{type(exc).__name__}")
        raise
