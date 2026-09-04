# chunking-autoresearch 一個月實驗結果：給 MVP1 完整版 chunker 參考

**日期**：2026-09-02
**寫給誰看**：負責 `AI Energy Operations Copilot`（MVP1）的 Claude Code，評估能不能把下面的優化方向實際套用到 production 的完整版 chunker（`spike/chunker.py` 或其後續版本）。
**狀態**：`chunking-autoresearch` 這個自動化實驗迴圈目前**已暫停**（原因見下方），這份報告是暫停前的成果整理。

## 這是什麼

`AI Benchmark and Evaluation Platform` 是 MVP1 的獨立姊妹專案，`tools/chunking_autoresearch/` 底下讓一個自主 AI agent（代號 Hermes）連續一個多月每晚自動改寫一份**簡化版**的 chunking 演算法（`strategy.py`，從 MVP1 的 `spike/chunker.py` 的 `structured_600_100` 策略簡化移植而來），透過固定的評分程式（`harness.py`）量測執行速度，摸索出一系列「讓 chunking 跑更快、同時不犧牲內容完整性跟切分品質」的具體改法。

**這不是要你直接套用一個 patch**——`strategy.py` 是簡化版（例如只做了一種表格偵測路徑，MVP1 完整版另外還有「標題在前、資料在後」的第二種偵測路徑），跟 MVP1 真正的 production chunker 程式碼結構不完全一樣。下面列的是**優化方向跟具體改法**，需要你對照 MVP1 實際的程式碼，評估能不能套用、套用後要用 MVP1 自己的測試／基準跑一次驗證，不要不驗證就直接搬過去用。

## 為什麼暫停

累積速度提升度長期卡在 14-15%，一直到不了原訂 20% 的目標。追查根因：用 `python -X importtime` 實測發現，就算把量測架構升級成「常駐 worker、重複測時共用同一份 import」（拿掉了原本佔 88.6% cost_time 的「每次重複計時都重開一個新 subprocess」固定成本），`strategy.chunk()` 這個唯一能改的函式邏輯本身，也只佔整個量到的執行時間裡一小部分。也就是說，**光靠改 chunking 演算法本身能拿到的效能空間，結構上就是有限的**——不是策略想得不夠好，是可優化的天花板本來就在那裡。

決定先暫停這個迴圈，把目前累積下來最好的成果整理成這份報告，看能不能在 MVP1 真正的 production 環境（有更複雜的邏輯、更大的資料量）裡找到比這個簡化版模擬環境更大的優化空間。**這不是永久關閉**——Hermes 的排程已暫停但設定保留，之後可能重啟。

## 具體策略改動（可直接參考、對照 MVP1 程式碼評估）

以下 5 個是目前累積下來最好的 `keep`（正規化後速度提升度，數字本身是用一個**已經被取代的舊量測架構**算出來的，架構已於 2026-09-01 升級，數字不能拿來跟未來新架構的結果直接比較——但**改動內容本身**跟量測架構無關，仍然有效）：

1. **未使用的 regex capturing group 改成 non-capturing group**（`_UNIT_PAREN_RE` 的例子：`(?:...)` 取代 `(...)`）——不改變匹配語意，只是省下 regex engine 記錄群組邊界的額外成本。如果 MVP1 的 chunker 裡有 regex 抓了東西但從來沒用過對應的 group 內容，這是零風險、直接可套用的優化。
2. **熱路徑（hot path）裡把會重複用到的值快取成區域變數**，避免每次都重新查找。具體例子：把 `_pack_with_overlap` 快取成區域變數；`lookback` 已經是新建的 list 就不要再包一層 `list(lookback)` 多餘複製。
3. **`CHUNK_SIZE=1500` 時把 `OVERLAP` 從 100 降到 75**——減少重疊部分造成的重複處理，同時確認仍然通過品質／內容保留門檻。這個要對照 MVP1 目前的 `CHUNK_SIZE`／`OVERLAP` 參數，看是否有類似的調整空間，但**務必先確認調整後內容覆蓋率跟切分品質沒有下降**，不是單純調小就好。
4. **常用常數／regex 從模組全域變數改成函式內區域變數**（Python 位元組碼層級：`LOAD_FAST`／`LOAD_DEREF` 比 `LOAD_GLOBAL` 快）。具體做法是把 `CHUNK_SIZE`、`OVERLAP` 跟常用的 regex pattern，在 `chunk()`／`_split_oversized()` 一開始就綁定成區域變數，而不是每次都去查模組全域。這是純粹的 CPython 直譯器層級優化，跟演算法邏輯無關，理論上可以直接套用到任何 Python 熱路徑。
5. **overlap 打包時，後續片段改用 iterator 走訪**（`iter(pieces[1:])`），避免建立額外的中間 list。

## 更大方向、可能對完整版更有價值的已驗證結論

比起上面幾個小修小補，這幾條方向性的發現，可能在 MVP1 真正的大量資料場景下效果更明顯：

- **`CHUNK_SIZE` 從 600 提高到 1500，能顯著減少 chunk 數量跟序列化資料量**——chunk 數量變少，代表要處理、要序列化、要塞進資料庫的物件數量也跟著變少，這個效應在小型模擬文件上就已經看得到，MVP1 處理真實大量文件時，效果應該會更明顯。**但要注意**：`CHUNK_SIZE` 調大也可能影響 embedding／檢索品質（單一 chunk 涵蓋的語意範圍變大），這是 chunking-autoresearch 這邊的簡化評分程式完全沒有評估的面向（它只看執行速度跟結構完整性，不看檢索準確度）——套用到 MVP1 之前，一定要用你們自己的檢索準確度基準重新驗證，不能只看「切分變快了」就直接上。
- **切分過長內容時，用 `finditer()` 搭配整數索引切割，不要先把多份句子字串具體化（materialize）出來**——避免建立不必要的中間字串物件。
- **組裝 chunk（對應 MVP1 的 `flush_para`／`flush_table` 或類似的收尾函式）時**：改用 list comprehension、直接取頭尾的 page range（不要逐一走訪算最大最小值）、把 `append` 方法快取成區域變數再重複呼叫、預先組好 `chunk_id` 的前綴字串。
- **呼叫 regex 之前先做一次便宜的字串條件檢查**（例如 `in`、`startswith`、長度檢查），能用簡單字串操作先篩掉的情況，就不要每次都跑一次 regex——regex 相對字串操作是有明顯開銷的。

## 給 MVP1 這邊 Claude Code 的建議下一步

1. 先讀過 MVP1 目前 chunker 的實際程式碼，逐條對照上面的優化方向，判斷哪些能直接套用、哪些需要調整。
2. 任何涉及參數調整（`CHUNK_SIZE`／`OVERLAP`）的改動，套用後都要用 MVP1 自己的檢索準確度基準重新驗證，不能只看執行速度——這是 chunking-autoresearch 那邊完全沒測過的面向。
3. 純語法糖／直譯器層級的優化（regex non-capturing group、區域變數快取、iterator 走訪）風險最低，可以優先考慮。
4. 如果套用後有實質進展、或想到新的優化方向想拿去自動化實驗迴圈裡測試，可以請人類評估要不要重新啟動 `chunking-autoresearch`（Hermes 的 cron job `588b84d20962` 目前是暫停、非刪除狀態）。

## 原始資料

完整實驗歷史在 `tools/chunking_autoresearch/results.tsv`（NAS 固定路徑，這個 repo 本機沒有真實資料副本）；機制設計細節見 `tools/chunking_autoresearch/program.md` 跟 `README.md`。
