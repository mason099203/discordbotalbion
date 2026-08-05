"""Discord Bot：監聽頻道中的 Albion 死亡連結並回覆；含 /createparty 報名"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

from albion_kill import DeathLink, fetch_kill_info, format_kill_message, parse_death_url
from display_settings import FIELD_LABELS, DisplaySettings
from item_localization import LOCALE_LABELS, ensure_loaded
from locale_settings import DEFAULT_LOCALE, locale_label, normalize_locale
from party import PartySignupView, SignupUserIds, register_createparty
from sheets import GoogleSheetClient, load_service_account_credentials

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
FALLBACK_MESSAGE = os.getenv("DEATH_LINK_REPLY", "無法取得死亡資訊")
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.json"))


def parse_spreadsheet_id(raw: str) -> str:
    """從完整 Google Sheet 網址或純 ID 取得試算表 ID。"""
    raw = raw.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if match:
        return match.group(1)
    return raw


GOOGLE_SHEET_ID = parse_spreadsheet_id(
    os.getenv("GOOGLE_SHEET_ID", "") or os.getenv("googlesheet_id", "")
)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

FIELD_CHOICES = [
    app_commands.Choice(name=label, value=key) for key, label in FIELD_LABELS.items()
]


@dataclass
class MonitorSetup:
    listen_channel_ids: list[int]
    reply_channel_id: int | None = None


def resolve_text_channel_id(
    channel: discord.abc.GuildChannel | discord.Thread,
) -> int | None:
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    if isinstance(channel, discord.TextChannel):
        return channel.id
    return None


def channel_ref(bot: discord.Client, channel_id: int) -> str:
    ch = bot.get_channel(channel_id)
    return ch.mention if ch else f"<#{channel_id}>"


def iter_message_urls(message: discord.Message) -> list[str]:
    urls: list[str] = []
    if message.content:
        urls.extend(URL_PATTERN.findall(message.content))
    for embed in message.embeds:
        if embed.url:
            urls.append(embed.url)
        if embed.description:
            urls.extend(URL_PATTERN.findall(embed.description))
        for field in embed.fields:
            urls.extend(URL_PATTERN.findall(field.value))
        if embed.footer and embed.footer.text:
            urls.extend(URL_PATTERN.findall(embed.footer.text))
        if embed.author and embed.author.url:
            urls.append(embed.author.url)
    return urls


def find_death_link_in_message(message: discord.Message) -> DeathLink | None:
    for url in iter_message_urls(message):
        link = parse_death_url(url)
        if link:
            return link
    return None


def format_listen_refs(bot: discord.Client, channel_ids: list[int]) -> str:
    return "、".join(channel_ref(bot, channel_id) for channel_id in channel_ids)


def log_monitor_status(bot: discord.Client, config: GuildConfig) -> None:
    setups = config._monitor
    print(f"已設定 {len(setups)} 個伺服器的監聽")
    if not setups:
        print("  （無）")
        return
    for guild_id, setup in setups.items():
        guild = bot.get_guild(guild_id)
        guild_name = guild.name if guild else f"未知({guild_id})"
        listen_ref = format_listen_refs(bot, setup.listen_channel_ids)
        if setup.reply_channel_id:
            reply_ref = channel_ref(bot, setup.reply_channel_id)
            print(f"  · {guild_name}：監聽 {listen_ref} → 回覆 {reply_ref}")
        else:
            print(f"  · {guild_name}：監聽 {listen_ref}（同頻道回覆）")


class GuildConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._monitor: dict[int, MonitorSetup] = {}
        self._display: dict[int, DisplaySettings] = {}
        self._locale: dict[int, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._monitor = {}
        if "monitor" in data:
            for gid, cfg in data["monitor"].items():
                listen_raw = cfg["listen"]
                if isinstance(listen_raw, list):
                    listen_ids = [int(channel_id) for channel_id in listen_raw]
                else:
                    listen_ids = [int(listen_raw)]
                self._monitor[int(gid)] = MonitorSetup(
                    listen_channel_ids=listen_ids,
                    reply_channel_id=int(cfg["reply"]) if cfg.get("reply") else None,
                )
        else:
            raw = data.get("channels", data.get("threads", {}))
            for gid, cid in raw.items():
                self._monitor[int(gid)] = MonitorSetup(listen_channel_ids=[int(cid)])

        self._display = {
            int(k): DisplaySettings.from_dict(v)
            for k, v in data.get("display", {}).items()
        }
        self._locale = {}
        for k, v in data.get("locale", {}).items():
            try:
                self._locale[int(k)] = normalize_locale(v)
            except ValueError:
                continue

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "monitor": {
                        str(gid): {
                            "listen": setup.listen_channel_ids,
                            "reply": setup.reply_channel_id,
                        }
                        for gid, setup in self._monitor.items()
                    },
                    "display": {
                        str(k): v.to_dict() for k, v in self._display.items()
                    },
                    "locale": {str(k): v for k, v in self._locale.items()},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def get_monitor(self, guild_id: int) -> MonitorSetup | None:
        setup = self._monitor.get(guild_id)
        if setup is None or not setup.listen_channel_ids:
            return None
        return setup

    def add_listen_channel(self, guild_id: int, channel_id: int) -> MonitorSetup:
        setup = self._monitor.get(guild_id)
        if setup:
            if channel_id not in setup.listen_channel_ids:
                setup.listen_channel_ids.append(channel_id)
        else:
            setup = MonitorSetup(listen_channel_ids=[channel_id])
            self._monitor[guild_id] = setup
        self.save()
        return setup

    def remove_listen_channel(self, guild_id: int, channel_id: int) -> bool:
        setup = self._monitor.get(guild_id)
        if setup is None or channel_id not in setup.listen_channel_ids:
            return False
        setup.listen_channel_ids.remove(channel_id)
        if not setup.listen_channel_ids:
            self._monitor.pop(guild_id, None)
        self.save()
        return True

    def set_reply_channel(self, guild_id: int, channel_id: int) -> MonitorSetup:
        setup = self._monitor.get(guild_id)
        if not setup:
            setup = MonitorSetup(
                listen_channel_ids=[channel_id], reply_channel_id=channel_id
            )
            self._monitor[guild_id] = setup
        else:
            setup.reply_channel_id = channel_id
        self.save()
        return setup

    def clear_reply_channel(self, guild_id: int) -> bool:
        setup = self._monitor.get(guild_id)
        if not setup or setup.reply_channel_id is None:
            return False
        setup.reply_channel_id = None
        self.save()
        return True

    def clear_monitor(self, guild_id: int) -> None:
        self._monitor.pop(guild_id, None)
        self.save()

    def should_monitor(self, guild_id: int, channel: discord.abc.GuildChannel) -> bool:
        setup = self.get_monitor(guild_id)
        if setup is None:
            return False
        listen_ids = set(setup.listen_channel_ids)
        if channel.id in listen_ids:
            return True
        if isinstance(channel, discord.Thread) and channel.parent_id in listen_ids:
            return True
        return False

    def get_reply_channel_id(
        self, guild_id: int, message_channel: discord.abc.MessageableChannel
    ) -> int:
        setup = self._monitor.get(guild_id)
        if setup and setup.reply_channel_id:
            return setup.reply_channel_id
        if isinstance(message_channel, discord.Thread):
            return message_channel.parent_id or message_channel.id
        return message_channel.id

    def get_display(self, guild_id: int) -> DisplaySettings:
        if guild_id not in self._display:
            self._display[guild_id] = DisplaySettings.default()
        return self._display[guild_id]

    def reset_display(self, guild_id: int) -> DisplaySettings:
        self._display[guild_id] = DisplaySettings.default()
        self.save()
        return self._display[guild_id]

    def save_display(self, guild_id: int) -> None:
        self.save()

    def get_locale(self, guild_id: int) -> str:
        return self._locale.get(guild_id, DEFAULT_LOCALE)

    def set_locale(self, guild_id: int, locale: str) -> str:
        locale = normalize_locale(locale)
        self._locale[guild_id] = locale
        self.save()
        return locale


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int, config: GuildConfig):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.config = config
        self._build_select()

    def _build_select(self) -> None:
        self.clear_items()
        settings = self.config.get_display(self.guild_id)
        options = [
            discord.SelectOption(
                label=label[:100],
                value=key,
                description="目前：開啟" if enabled else "目前：關閉",
            )
            for key, label, enabled in settings.iter_fields()
        ][:25]
        select = discord.ui.Select(
            placeholder="選擇要切換的欄位（點選即切換開關）",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_toggle
        self.add_item(select)

        reset_btn = discord.ui.Button(
            label="全部重設",
            style=discord.ButtonStyle.danger,
            emoji="🔄",
        )
        reset_btn.callback = self._on_reset
        self.add_item(reset_btn)

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        field = interaction.data["values"][0]  # type: ignore[index]
        settings = self.config.get_display(self.guild_id)
        settings.toggle(field)
        self.config.save_display(self.guild_id)
        self._build_select()
        label = FIELD_LABELS[field]
        state = "開啟" if getattr(settings, field) else "關閉"
        await interaction.response.edit_message(
            content=settings.format_summary(),
            view=self,
        )
        await interaction.followup.send(
            f"已將「{label}」設為 **{state}**。", ephemeral=True
        )

    async def _on_reset(self, interaction: discord.Interaction) -> None:
        settings = self.config.reset_display(self.guild_id)
        self._build_select()
        await interaction.response.edit_message(
            content=settings.format_summary(),
            view=self,
        )
        await interaction.followup.send("已重設為預設顯示欄位。", ephemeral=True)


class DeathLinkBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config = GuildConfig(CONFIG_PATH)
        self.sheets = GoogleSheetClient(GOOGLE_SHEET_ID)
        self.active_views: dict[int, PartySignupView] = {}
        self.signup_user_ids: SignupUserIds = {}

    def clear_signup_user_ids(self, sheet_name: str) -> None:
        for key in list(self.signup_user_ids):
            if key[0] == sheet_name:
                del self.signup_user_ids[key]

    async def refresh_sheet_views(self, sheet_name: str) -> None:
        for view in self.active_views.values():
            if view.sheet_name != sheet_name:
                continue
            try:
                await view.refresh_message()
            except discord.HTTPException:
                pass

    async def setup_hook(self) -> None:
        await asyncio.to_thread(ensure_loaded)
        await self.tree.sync()
        print(f"已同步 {len(self.tree.get_commands())} 個 slash 指令")

    async def on_ready(self) -> None:
        print(f"Bot 已上線：{self.user} (ID: {self.user.id})")
        log_monitor_status(self, self.config)

    async def _get_reply_channel(
        self, guild: discord.Guild, message_channel: discord.abc.MessageableChannel
    ) -> discord.abc.MessageableChannel:
        reply_id = self.config.get_reply_channel_id(guild.id, message_channel)
        if reply_id == message_channel.id:
            return message_channel
        channel = guild.get_channel(reply_id)
        if channel is None:
            channel = self.get_channel(reply_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return message_channel

    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.author.bot and message.author.id == self.user.id:
            return

        if not self.config.should_monitor(message.guild.id, message.channel):
            return

        if message.author.bot and not message.embeds:
            return

        link = find_death_link_in_message(message)
        if link is None:
            return

        settings = self.config.get_display(message.guild.id)
        locale = self.config.get_locale(message.guild.id)
        reply_channel = await self._get_reply_channel(message.guild, message.channel)
        async with reply_channel.typing():  # type: ignore[union-attr]
            info = await fetch_kill_info(link.kill_id, locale)
        if info:
            text = format_kill_message(info, link.official_url, settings)
            if text:
                await reply_channel.send(text)  # type: ignore[union-attr]
        else:
            await reply_channel.send(FALLBACK_MESSAGE)  # type: ignore[union-attr]


bot = DeathLinkBot()
register_createparty(bot)


settings_group = app_commands.Group(
    name="settings", description="調整死亡資訊顯示欄位"
)


@settings_group.command(name="show", description="查看目前顯示欄位設定")
async def settings_show(interaction: discord.Interaction) -> None:
    settings = bot.config.get_display(interaction.guild_id)  # type: ignore[arg-type]
    view = SettingsView(interaction.guild_id, bot.config)  # type: ignore[arg-type]
    await interaction.response.send_message(
        settings.format_summary(), view=view, ephemeral=True
    )


@settings_group.command(name="set", description="設定單一欄位開關")
@app_commands.describe(
    field="要調整的欄位",
    enabled="是否顯示",
)
@app_commands.choices(field=FIELD_CHOICES)
@app_commands.choices(
    enabled=[
        app_commands.Choice(name="開啟", value="on"),
        app_commands.Choice(name="關閉", value="off"),
    ]
)
async def settings_set(
    interaction: discord.Interaction,
    field: app_commands.Choice[str],
    enabled: app_commands.Choice[str],
):
    settings = bot.config.get_display(interaction.guild_id)  # type: ignore[arg-type]
    settings.set_field(field.value, enabled.value == "on")
    bot.config.save_display(interaction.guild_id)  # type: ignore[arg-type]
    state = "開啟" if enabled.value == "on" else "關閉"
    await interaction.response.send_message(
        f"已將「{field.name}」設為 **{state}**。\n\n{settings.format_summary()}",
        ephemeral=True,
    )


@settings_group.command(name="reset", description="重設為預設顯示欄位")
async def settings_reset(interaction: discord.Interaction) -> None:
    settings = bot.config.reset_display(interaction.guild_id)  # type: ignore[arg-type]
    await interaction.response.send_message(
        f"已重設為預設。\n\n{settings.format_summary()}",
        ephemeral=True,
    )


bot.tree.add_command(settings_group)


LANGUAGE_CHOICES = [
    app_commands.Choice(name=label, value=code)
    for code, label in LOCALE_LABELS.items()
]


@bot.tree.command(name="language", description="設定裝備名稱顯示語言")
@app_commands.describe(locale="裝備名稱語言")
@app_commands.choices(locale=LANGUAGE_CHOICES)
async def language(
    interaction: discord.Interaction,
    locale: app_commands.Choice[str],
) -> None:
    selected = bot.config.set_locale(interaction.guild_id, locale.value)  # type: ignore[arg-type]
    await interaction.response.send_message(
        f"已將裝備名稱語言設為 **{locale_label(selected)}**。",
        ephemeral=True,
    )


@bot.tree.command(name="languagestatus", description="查看目前裝備名稱語言")
async def languagestatus(interaction: discord.Interaction) -> None:
    locale = bot.config.get_locale(interaction.guild_id)  # type: ignore[arg-type]
    await interaction.response.send_message(
        f"目前裝備名稱語言：**{locale_label(locale)}**",
        ephemeral=True,
    )


@bot.tree.command(
    name="setmonitor",
    description="新增監聽頻道（在此頻道偵測死亡連結，可設定多個）",
)
async def setmonitor(interaction: discord.Interaction) -> None:
    channel_id = resolve_text_channel_id(interaction.channel)  # type: ignore[arg-type]
    if channel_id is None:
        await interaction.response.send_message(
            "請在**文字頻道**或**討論串**內執行此指令。", ephemeral=True
        )
        return

    setup = bot.config.add_listen_channel(interaction.guild_id, channel_id)  # type: ignore[arg-type]
    log_monitor_status(bot, bot.config)
    ref = channel_ref(bot, channel_id)
    listen_refs = format_listen_refs(bot, setup.listen_channel_ids)
    await interaction.response.send_message(
        f"已新增監聽頻道：{ref}（含該頻道內所有討論串）\n"
        f"目前監聽：{listen_refs}",
        ephemeral=True,
    )


@bot.tree.command(
    name="setreply",
    description="在回覆頻道設定監聽來源（死亡資訊將發送到此頻道）",
)
@app_commands.describe(
    listen="要監聽的文字頻道（偵測死亡連結）",
)
async def setreply(
    interaction: discord.Interaction,
    listen: discord.TextChannel,
) -> None:
    reply_id = resolve_text_channel_id(interaction.channel)  # type: ignore[arg-type]
    if reply_id is None:
        await interaction.response.send_message(
            "請在**文字頻道**或**討論串**內執行此指令。", ephemeral=True
        )
        return

    guild_id = interaction.guild_id  # type: ignore[assignment]
    setup = bot.config.add_listen_channel(guild_id, listen.id)
    bot.config.set_reply_channel(guild_id, reply_id)
    log_monitor_status(bot, bot.config)

    listen_refs = format_listen_refs(bot, setup.listen_channel_ids)
    reply_ref = channel_ref(bot, reply_id)
    await interaction.response.send_message(
        f"已設定回覆頻道：{reply_ref}\n"
        f"目前監聽：{listen_refs}（含各頻道內所有討論串）",
        ephemeral=True,
    )


@bot.tree.command(
    name="addmonitor",
    description="新增監聽頻道（可重複執行以監聽多個頻道）",
)
@app_commands.describe(channel="要新增監聽的文字頻道（未指定則為目前頻道）")
async def addmonitor(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
) -> None:
    if channel is not None:
        channel_id = channel.id
    else:
        channel_id = resolve_text_channel_id(interaction.channel)  # type: ignore[arg-type]
        if channel_id is None:
            await interaction.response.send_message(
                "請在**文字頻道**或**討論串**內執行此指令，或指定要監聽的頻道。",
                ephemeral=True,
            )
            return

    setup = bot.config.add_listen_channel(interaction.guild_id, channel_id)  # type: ignore[arg-type]
    log_monitor_status(bot, bot.config)
    ref = channel_ref(bot, channel_id)
    listen_refs = format_listen_refs(bot, setup.listen_channel_ids)
    await interaction.response.send_message(
        f"已新增監聽頻道：{ref}\n目前監聽：{listen_refs}",
        ephemeral=True,
    )


@bot.tree.command(
    name="removemonitor",
    description="移除單一監聽頻道",
)
@app_commands.describe(channel="要移除監聽的文字頻道（未指定則為目前頻道）")
async def removemonitor(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
) -> None:
    if channel is not None:
        channel_id = channel.id
    else:
        channel_id = resolve_text_channel_id(interaction.channel)  # type: ignore[arg-type]
        if channel_id is None:
            await interaction.response.send_message(
                "請在**文字頻道**或**討論串**內執行此指令，或指定要移除的頻道。",
                ephemeral=True,
            )
            return

    if not bot.config.remove_listen_channel(interaction.guild_id, channel_id):  # type: ignore[arg-type]
        await interaction.response.send_message("該頻道不在監聽清單中。", ephemeral=True)
        return

    log_monitor_status(bot, bot.config)
    ref = channel_ref(bot, channel_id)
    setup = bot.config.get_monitor(interaction.guild_id)  # type: ignore[arg-type]
    if setup:
        listen_refs = format_listen_refs(bot, setup.listen_channel_ids)
        msg = f"已移除監聽頻道：{ref}\n目前監聽：{listen_refs}"
    else:
        msg = f"已移除監聽頻道：{ref}\n目前已無監聽頻道。"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="clearreply",
    description="取消回覆頻道設定（改為在監聽頻道內直接回覆）",
)
async def clearreply(interaction: discord.Interaction) -> None:
    if not bot.config.clear_reply_channel(interaction.guild_id):  # type: ignore[arg-type]
        await interaction.response.send_message("尚未設定回覆頻道。", ephemeral=True)
        return

    log_monitor_status(bot, bot.config)
    await interaction.response.send_message(
        "已取消回覆頻道設定，將在監聽頻道內直接回覆。", ephemeral=True
    )


@bot.tree.command(
    name="clearmonitor",
    description="取消監聽與回覆頻道設定",
)
async def clearmonitor(interaction: discord.Interaction) -> None:
    if bot.config.get_monitor(interaction.guild_id) is None:  # type: ignore[arg-type]
        await interaction.response.send_message("尚未設定監聽頻道。", ephemeral=True)
        return

    bot.config.clear_monitor(interaction.guild_id)  # type: ignore[arg-type]
    log_monitor_status(bot, bot.config)
    await interaction.response.send_message("已取消所有監聽設定。", ephemeral=True)


@bot.tree.command(
    name="monitorstatus",
    description="查看目前監聽與回覆頻道",
)
async def monitorstatus(interaction: discord.Interaction) -> None:
    setup = bot.config.get_monitor(interaction.guild_id)  # type: ignore[arg-type]
    if setup is None:
        await interaction.response.send_message(
            "尚未設定。請在回覆頻道執行 `/setreply`，並選擇要監聽的頻道。",
            ephemeral=True,
        )
        return

    listen_refs = format_listen_refs(bot, setup.listen_channel_ids)
    if setup.reply_channel_id:
        reply_ref = channel_ref(bot, setup.reply_channel_id)
        msg = f"監聽頻道：{listen_refs}\n回覆頻道：{reply_ref}"
    else:
        msg = f"監聽頻道：{listen_refs}\n回覆頻道：同監聽頻道"
    await interaction.response.send_message(msg, ephemeral=True)


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("請在 .env 設定 DISCORD_TOKEN")
    if not GOOGLE_SHEET_ID:
        raise SystemExit("請在 .env 設定 GOOGLE_SHEET_ID")
    try:
        load_service_account_credentials()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise SystemExit(f"Google 憑證設定錯誤：{e}") from e
    try:
        bot.run(DISCORD_TOKEN)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "\n❌ 需要在 Discord Developer Portal 開啟 Message Content Intent：\n"
            "   1. https://discord.com/developers/applications\n"
            "   2. 選擇你的 Application（Token 必須來自同一個）\n"
            "   3. 左側 Bot → Privileged Gateway Intents\n"
            "   4. 開啟「MESSAGE CONTENT INTENT」\n"
            "   5. Save Changes 後重新執行 python bot.py\n"
        ) from None


if __name__ == "__main__":
    main()
