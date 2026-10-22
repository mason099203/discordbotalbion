# Albion 死亡連結 Bot

監聽指定 Discord 頻道，當有人貼上 Albion 擊殺板死亡連結時，自動查詢並回覆死亡資訊。

## 邀請 Bot 進伺服器

OAuth2 勾選 **`bot`**、**`applications.commands`**，Bot 權限使用：

**`274877974528`**

| 權限 | 用途 |
|------|------|
| 傳送訊息 | 回覆死亡連結 |
| 讀取訊息歷史 | 讀取討論串內容 |
| 在討論串中傳送訊息 | 在討論串內回覆 |

邀請連結格式（將 `你的CLIENT_ID` 換成 Application ID）：

```
https://discord.com/api/oauth2/authorize?client_id=你的CLIENT_ID&permissions=274877974528&scope=bot%20applications.commands
```

若 Bot 看不到討論串訊息，可加上「檢視頻道」權限，改用 `274877975552`（+1024）。

## 設定

1. 到 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Bot
2. **Bot** 分頁 → 開啟 **Message Content Intent** 與 **Server Members Intent**（依死者名標記成員）
3. 使用上方邀請連結將 Bot 加入伺服器
4. 複製 `.env.example` 為 `.env`，填入 `DISCORD_TOKEN`  
   並將 Regear 試算表分享給 Service Account（編輯者）

```env
DISCORD_TOKEN=你的Bot_Token
DEATH_LINK_REPLY=無法取得死亡資訊
ITEM_LOCALE=zh-TW
GOOGLE_SHEET_ID_REGEAR=你的Regear試算表ID
```

`DEATH_LINK_REPLY` 為 API 查詢失敗時的備用文字。

`ITEM_LOCALE` 為新伺服器的預設裝備語言（`en`、`zh-TW`、`zh-CN`），預設 `zh-TW`。

`GOOGLE_SHEET_ID_REGEAR` 為 Regear 申請與 Discord↔遊戲名綁定用的試算表。

## Regear 申請

| 指令 | 說明 |
|------|------|
| `/regear` | 貼上死亡連結申請（須 24 小時內、Kill ID 未申請過、申請人=死者） |
| `/bindname` | 綁定 Albion 遊戲名稱（寫入試算表 `bindings` 分頁） |
| `/mybind` | 查看自己目前綁定 |
| `/setregearchannel` | 設定申請成功後轉發的頻道（當日日期討論串） |
| `/clearregearchannel` | 取消轉發頻道 |
| `/regearchannelstatus` | 查看轉發頻道 |

監聽頻道回覆死亡資訊時會：

- 依死者遊戲名 `@` 對應的 Discord 成員（先查 `bindings`，再比對暱稱／顯示名）
- 附上「申請 Regear」按鈕（與 `/regear` 同一套驗證）

通過後寫入 `GOOGLE_SHEET_ID_REGEAR`，分頁為月份 `YYYY-MM`（例如 `2026-10`）。

名稱比對：有綁定則以綁定為準；無綁定則用 Discord 暱稱／顯示名。不符時會引導執行 `/bindname`。

多人同時申請時有併發上限（預設 2）與記憶體門檻（預設 RSS ≥ 220MB 拒絕）；忙碌時回覆「請稍後再試」。可在 `.env` 用 `REGEAR_MAX_CONCURRENT`、`REGEAR_MAX_RSS_MB` 調整。

## 綁定監聽與回覆頻道

可同時監聽**多個頻道**，並統一回覆到指定頻道。例如同時監聽 A、B，全部回覆到 B：

1. 到 **B 頻道**執行 `/setreply`，選擇要監聽的 **A 頻道**
2. 在 **B 頻道**再執行 `/addmonitor`（或 `/setmonitor`）→ 把 B 也加入監聽
3. 之後在 A 或 B 貼死亡連結，Bot 都會回覆到 B

| 指令 | 說明 |
|------|------|
| `/setreply` | 在回覆頻道執行，指定一個監聽頻道並設定回覆目標 |
| `/addmonitor` | 新增監聽頻道（可重複執行） |
| `/setmonitor` | 同 `/addmonitor`，在目前頻道新增監聽 |
| `/removemonitor` | 移除單一監聽頻道 |
| `/clearreply` | 取消回覆頻道，改為在監聽頻道直接回覆 |
| `/monitorstatus` | 查看監聽與回覆頻道 |
| `/clearmonitor` | 取消所有監聽設定 |

若只用 `/setmonitor` 而未設定 `/setreply`，則在監聽頻道內直接回覆。

也支援 **KillBoard#1 等 Bot 的 Embed 訊息**：若標題可點擊連到 `killboard-1.com/as/event/ID`（或官方擊殺板連結），Bot 會從 Embed 的 `url` 欄位讀取並回覆。

設定會保存在 `config.json`，重啟 Bot 後仍有效。

## 顯示欄位設定

| 指令 | 說明 |
|------|------|
| `/settings show` | 查看設定，並用下拉選單點選切換欄位 |
| `/settings set` | 問答方式設定單一欄位（選欄位 → 選開啟/關閉） |
| `/settings reset` | 重設為預設欄位 |

可調整的欄位：死者、擊殺者、IP、**全套裝備**、死亡價值、區域、參與人數、死亡時間、官方連結。

## 裝備名稱語言

裝備名稱資料來自 [ao-data/ao-bin-dumps](https://github.com/ao-data/ao-bin-dumps)（首次啟動下載後以 SQLite `data/item_names.db` 按需查詢，避免整表佔用記憶體）。

| 指令 | 說明 |
|------|------|
| `/language` | 設定裝備名稱語言（English / 繁體中文 / 简体中文） |
| `/languagestatus` | 查看目前語言設定 |

預設語言可由 `.env` 的 `ITEM_LOCALE` 設定（預設 `zh-TW`）。

## 安裝與執行

```bash
pip install -r requirements.txt
python bot.py
```

### 託管環境建議

- 請使用 **Python 3.11 或 3.12**（勿用 3.14；目前部分原生套件在 3.14 上較不穩，可能出現 `corrupted size vs. prev_size` / exit 134）。
- 記憶體建議 ≥ 256MB；已內建 Regear 併發與 RSS 閘門。

## 支援的連結格式

Bot 會自動辨識並轉換為官方擊殺板連結：

```
https://albiononline.com/en/killboard/kill/1059088405
https://albiononline.com/as/killboard/kill/497367118
https://killboard-1.com/as/event/497367118   → 自動轉為官方連結
```

## 注意

- 可分開設定監聽頻道與回覆頻道
- 每個伺服器一組監聽設定
- 請勿將 `.env` 提交至版本控制
