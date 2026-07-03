# Discord Party Bot (Albion)

透過 Google Sheet 建立 Discord 報名表單。管理員使用 slash 指令開啟表單，成員選擇位置後自動寫入 Sheet 並更新 Discord 訊息。

## 功能

- `/createparty`：讀取指定工作表的隊伍欄位，建立 Discord 報名表單
- 使用者從下拉選單選擇空位 → 寫入 Google Sheet → 即時更新 Discord embed
- 可取消自己的報名
- 到達設定時間後自動關閉報名

## Google Sheet 欄位對應

| 隊伍 | 選項欄（顯示職位/Build） | 報名欄（Bot 寫入名稱） | 職位欄 |
|------|------------------------|----------------------|--------|
| party1 | D6:D25 | B6:B25 | C6:C25 |
| party2 | L6:L25 | J6:J25 | K6:K25 |
| party3 | D34:D53 | B34:B53 | C34:C53 |
| party4 | L34:L53 | J34:J53 | K34:K53 |

D/L 欄為 Build 選項，Bot 會搭配 C/K 欄職位顯示（例如 `tank · 召喚巨劍...`）；B/J 欄由 Bot 寫入 Discord 顯示名稱。

## 設定步驟

### 1. Google Cloud Service Account

1. 到 [Google Cloud Console](https://console.cloud.google.com/) 建立專案
2. 啟用 **Google Sheets API**
3. 建立 **Service Account**，下載 JSON 金鑰，存為 `credentials.json`
4. 將 Service Account 的 email（例如 `xxx@xxx.iam.gserviceaccount.com`）加入試算表的「共用」編輯者

### 2. Discord Bot

1. 到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Application
2. Bot 分頁建立 Bot，複製 Token
3. 啟用 **Message Content Intent**（若需要）
4. OAuth2 → URL Generator：勾選 `bot`、`applications.commands`，邀請 Bot 進伺服器

### 3. 環境變數

複製 `.env.example` 為 `.env`：

```env
DISCORD_TOKEN=你的Bot_Token
GOOGLE_SHEET_ID=試算表網址中的ID
GOOGLE_CREDENTIALS=credentials.json
```

試算表 ID 為網址中 `/d/` 與 `/edit` 之間的字串。

### 4. 安裝與執行

```bash
pip install -r requirements.txt
python bot.py
```

## 使用方式

```
/createparty sheet_name:pt party:Party 1 time:2hr
```

| 參數 | 說明 | 範例 |
|------|------|------|
| sheet_name | 工作表分頁名稱 | `pt` |
| party | party1 ~ party4 | Party 1 |
| time | 關閉時間 | `2hr`、`30min`、`1h` |

## 注意事項

- Bot 重啟後，舊的報名訊息按鈕會失效，需重新 `/createparty`
- 取消報名僅能取消與自己 Discord 顯示名稱相同的欄位
- 請勿將 `credentials.json` 與 `.env` 提交至版本控制
