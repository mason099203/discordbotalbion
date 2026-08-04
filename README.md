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
2. **Bot** 分頁 → 開啟 **Message Content Intent**
3. 使用上方邀請連結將 Bot 加入伺服器
4. 複製 `.env.example` 為 `.env`，填入 `DISCORD_TOKEN`

```env
DISCORD_TOKEN=你的Bot_Token
DEATH_LINK_REPLY=無法取得死亡資訊
ITEM_LOCALE=zh-TW
```

`DEATH_LINK_REPLY` 為 API 查詢失敗時的備用文字。

`ITEM_LOCALE` 為新伺服器的預設裝備語言（`en`、`zh-TW`、`zh-CN`），預設 `zh-TW`。

## 綁定監聽與回覆頻道

可在 **B 頻道監聽**、在 **A 頻道回覆**：

1. 到 **A 頻道**（或 A 的討論串）執行 `/setreply`，並選擇要監聽的 **B 頻道**
2. 在 B 頻道貼死亡連結，Bot 會把資訊發到 A 頻道

| 指令 | 說明 |
|------|------|
| `/setreply` | 在回覆頻道執行，並指定要監聽的頻道 |
| `/setmonitor` | 僅設定監聽頻道（未設回覆時，在監聽頻道直接回覆） |
| `/clearreply` | 取消回覆頻道，改為在監聽頻道直接回覆 |
| `/monitorstatus` | 查看監聽與回覆頻道 |
| `/clearmonitor` | 取消所有監聽設定 |

若只用 `/setmonitor` 而未設定 `/setreply`，則在監聽頻道內直接回覆。

設定會保存在 `config.json`，重啟 Bot 後仍有效。

## 顯示欄位設定

| 指令 | 說明 |
|------|------|
| `/settings show` | 查看設定，並用下拉選單點選切換欄位 |
| `/settings set` | 問答方式設定單一欄位（選欄位 → 選開啟/關閉） |
| `/settings reset` | 重設為預設欄位 |

可調整的欄位：死者、擊殺者、IP、**全套裝備**、死亡價值、區域、參與人數、死亡時間、官方連結。

## 裝備名稱語言

裝備名稱資料來自 [ao-data/ao-bin-dumps](https://github.com/ao-data/ao-bin-dumps)（首次啟動會自動下載並快取至 `data/item_names.json`）。

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
