"""Regear 申請：/regear、/bindname、申請按鈕"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from albion_kill import (
    KillInfo,
    fetch_kill_info,
    format_equipment_bilingual_columns,
    format_equipment_bilingual_text,
    parse_death_url,
    to_official_kill_url,
)
from name_match import member_matches_game_name
from sheets import RegearSheetClient

REGEAR_TZ = ZoneInfo("Asia/Taipei")

# 同時處理上限（超出則回覆稍後再試）；RSS 超過門檻也拒絕新申請
REGEAR_MAX_CONCURRENT = max(1, int(os.getenv("REGEAR_MAX_CONCURRENT", "2")))
REGEAR_MAX_RSS_MB = max(64, int(os.getenv("REGEAR_MAX_RSS_MB", "220")))
BUSY_MESSAGE = "目前申請人數較多或負載偏高，請稍後再試。"

_regear_active = 0
_regear_gate = asyncio.Lock()


def _process_rss_mb() -> float | None:
    """目前行程 RSS（MB）；無法取得時回傳 None。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            GetCurrentProcess = ctypes.windll.kernel32.GetCurrentProcess
            GetProcessMemoryInfo = ctypes.windll.psapi.GetProcessMemoryInfo
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if GetProcessMemoryInfo(
                GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return counters.WorkingSetSize / (1024 * 1024)
        except Exception:  # noqa: BLE001
            return None
        return None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux: KB；macOS: bytes
        if sys.platform == "darwin":
            return usage / (1024 * 1024)
        return usage / 1024
    except Exception:  # noqa: BLE001
        return None


def _is_memory_high() -> bool:
    rss = _process_rss_mb()
    if rss is None:
        return False
    return rss >= REGEAR_MAX_RSS_MB

KILL_ID_LINE_RE = re.compile(r"Kill ID[:：]\s*(\d+)", re.IGNORECASE)
KILL_URL_RE = re.compile(
    r"(?:albiononline\.com(?:/[a-z]{2})?/killboard/kill/|killboard-1\.com(?:/[a-z]{2})?/event/)(\d+)",
    re.IGNORECASE,
)

REGEAR_BUTTON_CUSTOM_ID = "regear:apply"


class RegearHost(Protocol):
    regear_sheets: RegearSheetClient | None
    tree: app_commands.CommandTree

    def get_locale(self, guild_id: int) -> str: ...

    def get_regear_channel_id(self, guild_id: int) -> int | None: ...


def month_sheet_name(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def nearby_month_sheets(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    prev = now.replace(day=1) - timedelta(days=1)
    names = [month_sheet_name(now), month_sheet_name(prev)]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def format_ts(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class RegearResult:
    ok: bool
    message: str
    sheet_name: str | None = None


def extract_kill_id_from_message(content: str) -> int | None:
    match = KILL_ID_LINE_RE.search(content or "")
    if match:
        return int(match.group(1))
    match = KILL_URL_RE.search(content or "")
    if match:
        return int(match.group(1))
    return None


async def resolve_mentions_for_victim(
    guild: discord.Guild,
    sheets: RegearSheetClient,
    victim_name: str,
) -> list[discord.Member]:
    from name_match import find_members_by_game_name

    bound_ids = await asyncio.to_thread(
        sheets.get_discord_ids_for_game_name, victim_name
    )
    members: list[discord.Member] = []
    seen: set[int] = set()
    for uid in bound_ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        if member and member.id not in seen:
            members.append(member)
            seen.add(member.id)

    by_nick = await find_members_by_game_name(guild, victim_name)
    for member in by_nick:
        if member.id not in seen:
            members.append(member)
            seen.add(member.id)
    return members


def format_mention_prefix(members: list[discord.Member]) -> str:
    if not members:
        return ""
    return " ".join(m.mention for m in members) + "\n"


def daily_thread_name(now: datetime | None = None) -> str:
    now = now or datetime.now(REGEAR_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=REGEAR_TZ)
    else:
        now = now.astimezone(REGEAR_TZ)
    return now.strftime("%Y-%m-%d")


async def find_thread_by_name(
    channel: discord.TextChannel, name: str
) -> discord.Thread | None:
    for thread in channel.threads:
        if thread.name == name:
            return thread
    try:
        async for thread in channel.archived_threads(limit=100, private=False):
            if thread.name == name:
                return thread
    except discord.HTTPException:
        pass
    return None


async def get_or_create_daily_thread(
    channel: discord.TextChannel, name: str | None = None
) -> discord.Thread | None:
    thread_name = name or daily_thread_name()
    existing = await find_thread_by_name(channel, thread_name)
    if existing is not None:
        if existing.archived:
            try:
                await existing.edit(archived=False)
            except discord.HTTPException:
                pass
        return existing

    try:
        return await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
            reason="Regear 當日申請討論串",
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"建立 Regear 討論串失敗：{e}")
        return None


async def forward_regear_application(
    host: RegearHost,
    guild: discord.Guild,
    member: discord.Member,
    message: str,
) -> str | None:
    """轉發至設定頻道的當日討論串。成功回傳討論串提及，失敗回傳錯誤說明。"""
    channel_id = host.get_regear_channel_id(guild.id)
    if channel_id is None:
        return None

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await guild.fetch_channel(channel_id)
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except discord.HTTPException:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return "轉發失敗：找不到設定的 Regear 頻道。"

    thread = await get_or_create_daily_thread(channel)
    if thread is None:
        return "轉發失敗：無法建立或開啟當日討論串。"

    content = f"{member.mention}\n{message}"
    if len(content) > 2000:
        content = content[:1990] + "\n…"
    try:
        await thread.send(content)
    except discord.HTTPException as e:
        return f"轉發失敗：{e}"
    return f"已轉發至 {thread.mention}"


async def publish_regear_result(
    interaction: discord.Interaction,
    host: RegearHost,
    member: discord.Member,
    result: RegearResult,
) -> None:
    """申請回覆僅申請人可見；成功時轉發至當日討論串（公開）。"""
    reply = result.message
    if result.ok and interaction.guild is not None:
        forward_note = await forward_regear_application(
            host, interaction.guild, member, result.message
        )
        if forward_note:
            reply = f"{reply}\n{forward_note}"
    await interaction.followup.send(reply, ephemeral=True)


def append_kill_id_footer(text: str, kill_id: int) -> str:
    if extract_kill_id_from_message(text) is not None:
        return text
    return f"{text}\nKill ID: {kill_id}"


async def process_regear_application(
    *,
    sheets: RegearSheetClient,
    member: discord.Member,
    kill_id: int,
    locale: str,
) -> RegearResult:
    """含併發／記憶體閘門；忙碌時立即回覆稍後再試。"""
    global _regear_active

    async with _regear_gate:
        if _regear_active >= REGEAR_MAX_CONCURRENT or _is_memory_high():
            return RegearResult(False, BUSY_MESSAGE)
        _regear_active += 1

    try:
        if _is_memory_high():
            return RegearResult(False, BUSY_MESSAGE)
        return await _process_regear_application_inner(
            sheets=sheets,
            member=member,
            kill_id=kill_id,
            locale=locale,
        )
    finally:
        async with _regear_gate:
            _regear_active = max(0, _regear_active - 1)


async def _process_regear_application_inner(
    *,
    sheets: RegearSheetClient,
    member: discord.Member,
    kill_id: int,
    locale: str,
) -> RegearResult:
    info = await fetch_kill_info(kill_id, locale)
    if info is None:
        return RegearResult(False, "無法取得死亡資訊，請確認連結是否正確。")

    now = datetime.now(timezone.utc)
    if info.timestamp is None:
        return RegearResult(False, "無法確認死亡時間，無法申請 Regear。")

    death_ts = info.timestamp
    if death_ts.tzinfo is None:
        death_ts = death_ts.replace(tzinfo=timezone.utc)
    else:
        death_ts = death_ts.astimezone(timezone.utc)

    if now - death_ts > timedelta(hours=24):
        return RegearResult(
            False,
            f"死亡時間已超過 24 小時（死亡：{format_ts(death_ts)}），無法申請。",
        )

    if await asyncio.to_thread(
        sheets.kill_id_exists, kill_id, nearby_month_sheets(now)
    ):
        return RegearResult(False, f"Kill ID `{kill_id}` 已申請過，不可重複申請。")

    victim = info.victim_name.strip()
    bound = await asyncio.to_thread(sheets.get_binding, member.id)
    if bound:
        if bound.strip().casefold() != victim.casefold():
            return RegearResult(
                False,
                f"綁定遊戲名 **{bound.strip()}** 與死者 **{victim}** 不符。\n"
                f"請用 `/bindname {victim}` 更正後再申請。",
            )
    elif not member_matches_game_name(member, victim):
        return RegearResult(
            False,
            f"無法確認你是死者 **{victim}**。\n"
            f"請先執行 `/bindname {victim}` 綁定遊戲名稱後再申請。",
        )

    sheet_name = month_sheet_name(now)
    official_url = to_official_kill_url(kill_id)
    zh_locale = locale if locale.startswith("zh") else "zh-TW"
    row = build_regear_row(
        now=now,
        member=member,
        game_name=victim,
        info=info,
        official_url=official_url,
        zh_locale=zh_locale,
    )
    try:
        await asyncio.to_thread(sheets.append_regear_row, sheet_name, row)
    except Exception as e:  # noqa: BLE001
        return RegearResult(False, f"寫入 Google Sheet 失敗：{e}")

    equip_text = format_equipment_bilingual_text(
        info.victim_equipment_raw, zh_locale=zh_locale
    )
    parts = [f"✅ **Regear 已登記**（Kill ID `{kill_id}`）"]
    if equip_text:
        parts.append(equip_text)
    message = "\n".join(parts)
    if len(message) > 1900:
        message = message[:1890] + "\n…"
    return RegearResult(True, message, sheet_name=sheet_name)


def build_regear_row(
    *,
    now: datetime,
    member: discord.Member,
    game_name: str,
    info: KillInfo,
    official_url: str,
    zh_locale: str = "zh-TW",
) -> list[str]:
    equip_cols = format_equipment_bilingual_columns(
        info.victim_equipment_raw, zh_locale=zh_locale
    )
    return [
        format_ts(now),
        str(member.id),
        member.display_name,
        game_name,
        str(info.kill_id),
        info.victim_name,
        info.killer_name,
        f"{info.victim_ip:.0f}",
        f"{info.killer_ip:.0f}",
        *equip_cols,
        str(info.death_fame),
        info.kill_area,
        format_ts(info.timestamp),
        official_url,
    ]


class RegearApplyView(discord.ui.View):
    """永久按鈕；從訊息內容解析 Kill ID。"""

    def __init__(self, host: RegearHost | None = None) -> None:
        super().__init__(timeout=None)
        self.host = host

    @discord.ui.button(
        label="申請 Regear",
        style=discord.ButtonStyle.success,
        custom_id=REGEAR_BUTTON_CUSTOM_ID,
    )
    async def apply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        host = self.host
        if host is None or host.regear_sheets is None:
            await interaction.response.send_message(
                "Regear 尚未設定（請設定 GOOGLE_SHEET_ID_REGEAR）。",
                ephemeral=True,
            )
            return

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "請在伺服器內使用此按鈕。", ephemeral=True
            )
            return

        content = interaction.message.content if interaction.message else ""
        kill_id = extract_kill_id_from_message(content)
        if kill_id is None:
            await interaction.response.send_message(
                "無法從此訊息讀取 Kill ID，請改用 `/regear` 並貼上死亡連結。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        locale = host.get_locale(interaction.guild.id)
        result = await process_regear_application(
            sheets=host.regear_sheets,
            member=interaction.user,
            kill_id=kill_id,
            locale=locale,
        )
        await publish_regear_result(interaction, host, interaction.user, result)


def register_regear(bot: RegearHost) -> None:
    @bot.tree.command(name="bindname", description="綁定你的 Albion 遊戲名稱")
    @app_commands.describe(game_name="遊戲內角色名稱（需與擊殺板死者名一致）")
    async def bindname(interaction: discord.Interaction, game_name: str) -> None:
        if bot.regear_sheets is None:
            await interaction.response.send_message(
                "尚未設定 GOOGLE_SHEET_ID_REGEAR。", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "請在伺服器內使用此指令。", ephemeral=True
            )
            return

        name = game_name.strip()
        if not name:
            await interaction.response.send_message(
                "遊戲名稱不可為空。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        now = format_ts(datetime.now(timezone.utc))
        try:
            await asyncio.to_thread(
                bot.regear_sheets.upsert_binding,
                interaction.user.id,
                interaction.user.display_name,
                name,
                now,
            )
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"寫入綁定失敗：{e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"已綁定遊戲名稱：**{name}**（Discord：{interaction.user.mention}）",
            ephemeral=True,
        )

    @bot.tree.command(name="mybind", description="查看目前綁定的遊戲名稱")
    async def mybind(interaction: discord.Interaction) -> None:
        if bot.regear_sheets is None:
            await interaction.response.send_message(
                "尚未設定 GOOGLE_SHEET_ID_REGEAR。", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        bound = await asyncio.to_thread(
            bot.regear_sheets.get_binding, interaction.user.id
        )
        if bound:
            await interaction.followup.send(f"目前綁定：**{bound}**", ephemeral=True)
        else:
            await interaction.followup.send(
                "尚未綁定。請使用 `/bindname 遊戲名`。\n"
                "未綁定時會嘗試用你的 Discord 暱稱／顯示名比對死者名。",
                ephemeral=True,
            )

    @bot.tree.command(name="regear", description="申請 Regear（貼上死亡連結）")
    @app_commands.describe(url="Albion 擊殺板或 KillBoard#1 死亡連結")
    async def regear_cmd(interaction: discord.Interaction, url: str) -> None:
        if bot.regear_sheets is None:
            await interaction.response.send_message(
                "尚未設定 GOOGLE_SHEET_ID_REGEAR。", ephemeral=True
            )
            return
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "請在伺服器內使用此指令。", ephemeral=True
            )
            return

        link = parse_death_url(url.strip())
        if link is None:
            await interaction.response.send_message(
                "無法辨識死亡連結，請貼上官方擊殺板或 KillBoard#1 連結。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        locale = bot.get_locale(interaction.guild.id)
        result = await process_regear_application(
            sheets=bot.regear_sheets,
            member=interaction.user,
            kill_id=link.kill_id,
            locale=locale,
        )
        await publish_regear_result(interaction, bot, interaction.user, result)
