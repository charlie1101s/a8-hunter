# A8 Hunter 每日掃描（網頁版）

只保留 A8 Hunter V4.0 的 `is_A8_Setup` 邏輯（參數與 Pine 版預設相同），每個交易日收盤後由 GitHub 自動掃描，結果產生成一個網頁，你有空時打開網址就能看。

- **台灣50**：`tickers_tw50.txt` 的名單，台北時間週一到週五 15:10 更新
- **SPY 權重前 50 大**：每次自動從 SSGA 官網抓最新排序，台北時間週二到週六 06:20 更新（美股收盤後）

網頁內容（台股／美股兩個分頁）：
1. **今日 A8 訊號**：最新一根日K出現 A8 的股票
2. **近 20 個交易日出現過 A8**：訊號日價格 → 現價漲跌
3. **距離進場區**：全部 50 檔依「收盤 ÷ 0.7×EMA365」排序，看誰已在獵殺區、誰還差多少

## 設定（約 10 分鐘，一次性）

1. 在 GitHub 建一個 **Public** repo（免費帳號的 GitHub Pages 只支援 Public；裡面只有程式和掃描結果，沒有任何個人資料），把這個資料夾全部檔案上傳，包含 `.github/workflows/a8-scan.yml`
2. Repo → **Actions** 分頁 → A8 Hunter daily scan → **Run workflow**（market 選 `all`），等 2～3 分鐘跑完，repo 裡會多一個 `docs` 資料夾
3. Repo → **Settings → Pages** → Source 選 **Deploy from a branch**，Branch 選 `main`、資料夾選 `/docs` → Save
4. 一兩分鐘後，網址會是 `https://<你的帳號>.github.io/<repo名稱>/`，加到手機主畫面即可

之後每天自動更新，不用再管它。

### 選用：出現訊號時也推播到 Telegram
Settings → Secrets and variables → Actions：
- Secrets：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（在 Telegram 找 @BotFather 建機器人取得）
- Variables：`PAGE_URL` = 你的網頁網址（推播訊息會附上連結）

沒設定就只更新網頁，不推播。

## 本機使用

```bash
pip install -r requirements.txt
python a8_scanner.py all --no-telegram      # 掃描並產生 docs/index.html
python a8_scanner.py --history 2330.TW      # 列出某檔歷史上所有 A8 訊號，跟 TradingView 日線對照
```

## 注意事項

- **用日線計算**：EMA365 = 365 根交易日，大約 1.5 年，不是日曆上的一年。這跟 TradingView 日線圖上看到的一致。
- **價格處理**：除權息不還原、分割有調整，與 TradingView 預設相同。
- **台股名單要手動更新**：每季（3、6、9、12 月）換股後要更新 `tickers_tw50.txt`。美股名單是自動的。
- **掃描失敗的處理**：GitHub 會寄信通知你，網頁維持上一次的結果。網頁上方會顯示 K 線日期，可以看出資料有沒有更新。
- **排程延遲**：GitHub 的排程可能延遲幾分鐘到一小時，屬正常現象。
