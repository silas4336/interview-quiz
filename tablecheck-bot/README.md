# TableCheck 自動訂位機器人

自動在每天開放訂位的時刻（預設**日本時間 21:00**)準時上線、用快速輪詢搶下
指定餐廳（如 [Ramen Break Beats](https://www.tablecheck.com/en/shops/ramenbreakbeats/reserve)）
的訂位時段,省去每天手動搶位、拚手速的痛苦。

用 [Playwright](https://playwright.dev/) 驅動真正的訂位頁面,跟真人操作完全一樣
(包含登入、選人數、選日期/時段、走到付款),所以不需要官方 API 權限。

> ⚠️ **使用前請讀**
> - 自動化操作**可能違反 TableCheck 服務條款**,有帳號被封鎖的風險,使用風險自負。
> - 請勿把輪詢間隔調得太誇張而對伺服器造成負擔(本工具已採克制的間隔)。
> - 這家店**每人有不可退訂金**,送出訂位等於要付款,請理解後再用。
> - 網站改版時前端選擇器可能需要微調,可用 `--inspect` 協助校正。

## 安裝

需要 Python 3.11+(用到 `zoneinfo`)。

```bash
cd tablecheck-bot
python -m venv .venv && source .venv/bin/activate   # 選用
pip install -r requirements.txt
playwright install chromium
```

## 設定

```bash
cp config.example.yaml config.yaml
```

編輯 `config.yaml`,重點欄位:

| 欄位 | 說明 |
|------|------|
| `shop_slug` | 餐廳網址代稱,例如 `ramenbreakbeats` |
| `open_time` / `timezone` | 開放訂位的時刻與時區(預設 `21:00:00` / `Asia/Tokyo`) |
| `target_days_ahead` | 目標日期離今天幾天(預設 7) |
| `party_size` | 用餐人數 |
| `preferred_times` | 想要的時段(依優先順序) |
| `stop_before_payment` | `true`=停在付款前由你手動付(較安全);`false`=一路自動送出(需帳號已綁付款方式) |

`config.yaml` 已被 `.gitignore` 忽略,不會被 commit,可放心填個人資料。

## 使用

```bash
# 1) 先手動登入一次(session 會被記住,之後搶位免登入、最快)
python bot.py --login

# 2) 排程搶位:程式會一直等到下一個開放時刻才動作
python bot.py

# 立刻測試一次(不等待)
python bot.py --now

# 校正選擇器(開啟頁面 + Playwright Inspector)
python bot.py --inspect
```

建議流程:先 `--login`,再用 `--now` 在非開放時段跑一次確認流程能跑到哪一步、
選擇器對不對,確認沒問題後,於開放前啟動 `python bot.py` 讓它準時搶。

也可以用 cron / 排程器在每天 20:55 自動啟動。

## 運作方式

1. **暖機**:在開放前 `warmup_seconds` 秒載入頁面、預先選好人數。
2. **精準對時**:用 busy-wait 等到整點(毫秒級)。
3. **搶位輪詢**:在 `attempt_window_seconds` 秒內反覆嘗試選日期→選時段;
   日期還沒開放或時段沒空位就立刻重試。
4. **完成**:搶到後依 `stop_before_payment` 設定停在付款前或自動送出,並發出通知。

## 疑難排解

- **找不到人數/日期/時段** → 網站可能改版。執行 `python bot.py --inspect`,
  用 Inspector 找出正確的文字/屬性,再到 `bot.py` 的對應函式(`select_party_size`、
  `select_date`、`pick_time_slot`)調整選擇器。
- **每次都要重新登入** → 確認 `.browser-profile/` 資料夾沒被刪除(它存著 session)。
- **被判定為機器人** → 保持有頭模式(預設)、不要把視窗關掉、不要把間隔調太密。
