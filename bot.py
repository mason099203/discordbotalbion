"""Discord Bot 主程式"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv

from sheets import (
    GoogleSheetClient,
    PartyId,
    Slot,
    encode_signup,
    parse_signup,
    signup_owned_by,
)

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")


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
SHEET_TEMPLATE_NAME = os.getenv("SHEET_TEMPLATE_NAME", "範本")


def parse_id_list(env_key: str) -> set[int]:
    raw = os.getenv(env_key, "")
    ids: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            ids.add(int(part))
    return ids


CANCEL_MANAGER_USER_IDS = parse_id_list("CANCEL_MANAGER_USER_IDS")
CANCEL_MANAGER_ROLE_IDS = parse_id_list("CANCEL_MANAGER_ROLE_IDS")
CANCEL_ALLOW_MANAGE_MESSAGES = os.getenv(
    "CANCEL_ALLOW_MANAGE_MESSAGES", ""
).lower() in ("1", "true", "yes")


def can_cancel_signup(
    interaction: discord.Interaction, signup_value: str
) -> bool:
    if signup_owned_by(
        signup_value, interaction.user.id, interaction.user.display_name
    ):
        return True

    user = interaction.user
    if user.id in CANCEL_MANAGER_USER_IDS:
        return True

    if isinstance(user, discord.Member):
        perms = user.guild_permissions
        if perms.administrator:
            return True
        if CANCEL_ALLOW_MANAGE_MESSAGES and perms.manage_messages:
            return True
        if {role.id for role in user.roles} & CANCEL_MANAGER_ROLE_IDS:
            return True

    return False


def can_manage_party(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if user.id in CANCEL_MANAGER_USER_IDS:
        return True

    if isinstance(user, discord.Member):
        perms = user.guild_permissions
        if perms.administrator:
            return True
        if CANCEL_ALLOW_MANAGE_MESSAGES and perms.manage_messages:
            return True
        if {role.id for role in user.roles} & CANCEL_MANAGER_ROLE_IDS:
            return True

    return False

PARTY_LABELS = {
    "party1": "Party 1",
    "party2": "Party 2",
    "party3": "Party 3",
    "party4": "Party 4",
}


def parse_duration(text: str) -> timedelta | None:
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*(hr|hrs|hour|hours|h|min|mins|minute|minutes|m)", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("h"):
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def format_slots_table(slots: list[Slot]) -> str:
    lines = ["職位\tBuild\t報名者"]
    for s in slots:
        role = s.role or "-"
        lines.append(f"{role}\t{s.build}\t{s.display_value}")
    return "\n".join(lines)


def format_slots_embed(
    slots: list[Slot],
    sheet_name: str,
    party: PartyId,
    closes_at: datetime | None,
    closed: bool = False,
    title: str | None = None,
    note: str | None = None,
) -> discord.Embed:
    embed_title = title or f"{PARTY_LABELS[party]} 報名表"

    desc_parts: list[str] = []
    if note:
        desc_parts.append(f"📌 {note}")
    desc_parts.append(f"工作表：`{sheet_name}`")

    embed = discord.Embed(
        title=embed_title,
        description="\n".join(desc_parts),
        color=discord.Color.red() if closed else discord.Color.green(),
    )

    table = format_slots_table(slots) if slots else "無資料"
    embed.add_field(name="名額", value=table, inline=False)

    if closed:
        embed.set_footer(text="報名已關閉")
    elif closes_at:
        ts = int(closes_at.timestamp())
        embed.set_footer(text="每人限報一個位置；重選將自動取消原報名")
        embed.add_field(
            name="關閉時間",
            value=f"<t:{ts}:F> (<t:{ts}:R>)",
            inline=False,
        )
    else:
        embed.set_footer(text="每人限報一個位置；重選將自動取消原報名")

    return embed


class PartySignupView(discord.ui.View):
    def __init__(
        self,
        bot: "PartyBot",
        sheet_name: str,
        party: PartyId,
        closes_at: datetime | None,
        title: str | None = None,
        note: str | None = None,
        message: discord.Message | None = None,
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.sheet_name = sheet_name
        self.party = party
        self.closes_at = closes_at
        self.title = title
        self.note = note
        self.message = message
        self.closed = False
        self.auto_close_task: asyncio.Task | None = None
        self._build_select()

    def _is_expired(self) -> bool:
        if self.closed:
            return True
        if self.closes_at and datetime.now(timezone.utc) >= self.closes_at:
            return True
        return False

    def _build_select(self) -> None:
        self.clear_items()
        if not self._is_expired():
            slots = self.bot.sheets.read_party_slots(self.sheet_name, self.party)
            available = [s for s in slots if not s.value]
            if available:
                options = [
                    discord.SelectOption(label=s.label[:100], value=str(s.index))
                    for s in available[:25]
                ]
                select = discord.ui.Select(
                    placeholder="選擇要報名的位置",
                    options=options,
                    custom_id=f"party_signup:{self.sheet_name}:{self.party}",
                )
                select.callback = self._on_select
                self.add_item(select)

            taken = [s for s in slots if s.value]
            if taken:
                leave_options = [
                    discord.SelectOption(
                        label=f"取消 {s.label} ({parse_signup(s.value)[0] or s.display_value})"[
                            :100
                        ],
                        value=str(s.index),
                    )
                    for s in taken[:25]
                ]
                leave_select = discord.ui.Select(
                    placeholder="取消報名（本人或管理員）",
                    options=leave_options,
                    custom_id=f"party_leave:{self.sheet_name}:{self.party}",
                )
                leave_select.callback = self._on_leave
                self.add_item(leave_select)

        refresh_btn = discord.ui.Button(
            label="更新",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            custom_id=f"party_refresh:{self.sheet_name}:{self.party}",
        )
        refresh_btn.callback = self._on_refresh
        self.add_item(refresh_btn)

        delete_btn = discord.ui.Button(
            label="刪除表單",
            style=discord.ButtonStyle.danger,
            emoji="🗑️",
            custom_id=f"party_delete:{self.sheet_name}:{self.party}",
        )
        delete_btn.callback = self._on_delete
        self.add_item(delete_btn)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if self._is_expired():
            await interaction.response.send_message("報名已關閉。", ephemeral=True)
            return

        index = int(interaction.data["values"][0])  # type: ignore[index]
        slots = self.bot.sheets.read_party_slots(self.sheet_name, self.party)
        slot = next((s for s in slots if s.index == index), None)
        if not slot:
            await interaction.response.send_message("找不到該位置。", ephemeral=True)
            return
        if slot.value:
            await interaction.response.send_message(
                f"**{slot.label}** 已被 {slot.display_value} 報名。",
                ephemeral=True,
            )
            return

        user = interaction.user
        existing = next(
            (s for s in slots if signup_owned_by(s.value, user.id, user.display_name)),
            None,
        )
        if existing:
            self.bot.sheets.clear_slot_value(self.sheet_name, existing.value_cell)

        self.bot.sheets.write_slot_value(
            self.sheet_name,
            slot.value_cell,
            encode_signup(user.display_name, user.id),
        )

        if existing and existing.index != index:
            await interaction.response.send_message(
                f"已取消 **{existing.label}** 的報名，改報 **{slot.label}**！",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"已成功報名 **{slot.label}**！", ephemeral=True
            )
        await self.refresh_message()

    async def _on_leave(self, interaction: discord.Interaction) -> None:
        if self._is_expired():
            await interaction.response.send_message("報名已關閉。", ephemeral=True)
            return

        index = int(interaction.data["values"][0])  # type: ignore[index]
        slots = self.bot.sheets.read_party_slots(self.sheet_name, self.party)
        slot = next((s for s in slots if s.index == index), None)
        if not slot or not slot.value:
            await interaction.response.send_message("找不到報名紀錄。", ephemeral=True)
            return

        if not can_cancel_signup(interaction, slot.value):
            await interaction.response.send_message(
                "只能取消自己的報名。", ephemeral=True
            )
            return

        self.bot.sheets.clear_slot_value(self.sheet_name, slot.value_cell)
        if signup_owned_by(
            slot.value, interaction.user.id, interaction.user.display_name
        ):
            msg = f"已取消 **{slot.label}** 的報名。"
        else:
            msg = f"已取消 **{slot.label}**（{slot.display_value}）的報名。"
        await interaction.response.send_message(msg, ephemeral=True)
        await self.refresh_message()

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        slots = self.bot.sheets.read_party_slots(self.sheet_name, self.party)
        self._build_select()
        embed = format_slots_embed(
            slots,
            self.sheet_name,
            self.party,
            self.closes_at,
            self.closed,
            self.title,
            self.note,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.message = interaction.message

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        if not can_manage_party(interaction):
            await interaction.response.send_message(
                "你沒有刪除表單的權限。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        protected = {SHEET_TEMPLATE_NAME, "範本"}
        sheet_msg = ""
        if self.sheet_name not in protected:
            try:
                self.bot.sheets.delete_sheet_tab(self.sheet_name, protected)
                sheet_msg = f"已刪除工作表 `{self.sheet_name}`。"
            except Exception as e:
                sheet_msg = f"工作表刪除失敗：{e}"

        if self.auto_close_task and not self.auto_close_task.done():
            self.auto_close_task.cancel()

        if self.message and self.message.id in self.bot.active_views:
            del self.bot.active_views[self.message.id]

        if interaction.message:
            await interaction.message.delete()

        await interaction.followup.send(
            f"已刪除報名表單。{sheet_msg}".strip(), ephemeral=True
        )

    async def refresh_message(self) -> None:
        if not self.message:
            return
        slots = self.bot.sheets.read_party_slots(self.sheet_name, self.party)
        self._build_select()
        embed = format_slots_embed(
            slots,
            self.sheet_name,
            self.party,
            self.closes_at,
            self.closed,
            self.title,
            self.note,
        )
        await self.message.edit(embed=embed, view=self)

    async def close_signup(self) -> None:
        self.closed = True
        await self.refresh_message()


class PartyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.sheets = GoogleSheetClient(CREDENTIALS_PATH, GOOGLE_SHEET_ID)
        self.active_views: dict[int, PartySignupView] = {}

    async def setup_hook(self) -> None:
        await self.tree.sync()
        print(f"已同步 {len(self.tree.get_commands())} 個 slash 指令")

    async def on_ready(self) -> None:
        print(f"Bot 已上線：{self.user} (ID: {self.user.id})")


bot = PartyBot()


@bot.tree.command(
    name="createparty",
    description="從 Google Sheet 建立報名表單",
)
@app_commands.describe(
    sheet_name="Google Sheet 工作表名稱",
    party="要開啟的隊伍 (party1~party4)",
    time="關閉時間，例如 2hr、30min",
    title="報名表標題（選填）",
    note="備註說明（選填）",
)
@app_commands.choices(
    party=[
        app_commands.Choice(name="Party 1", value="party1"),
        app_commands.Choice(name="Party 2", value="party2"),
        app_commands.Choice(name="Party 3", value="party3"),
        app_commands.Choice(name="Party 4", value="party4"),
    ]
)
async def createparty(
    interaction: discord.Interaction,
    sheet_name: str,
    party: app_commands.Choice[str],
    time: str,
    title: str | None = None,
    note: str | None = None,
):
    await interaction.response.defer()

    duration = parse_duration(time)
    if duration is None:
        await interaction.followup.send(
            "時間格式錯誤，請使用例如 `2hr`、`1h`、`30min`。", ephemeral=True
        )
        return

    party_id: PartyId = party.value  # type: ignore[assignment]

    sheet_created = False
    try:
        sheet_created = bot.sheets.ensure_sheet_from_template(
            sheet_name, SHEET_TEMPLATE_NAME
        )
        slots = bot.sheets.read_party_slots(sheet_name, party_id)
    except Exception as e:
        await interaction.followup.send(
            f"讀取 Google Sheet 失敗：{e}\n"
            f"請確認範本工作表「{SHEET_TEMPLATE_NAME}」存在，且 Service Account 有權限。",
            ephemeral=True,
        )
        return

    if not slots:
        await interaction.followup.send(
            f"在工作表 `{sheet_name}` 的 {PARTY_LABELS[party_id]} 區域找不到名額資料。",
            ephemeral=True,
        )
        return

    if sheet_created and not note:
        note = f"已從「{SHEET_TEMPLATE_NAME}」建立新工作表"
    elif sheet_created and note:
        note = f"{note}\n（已從「{SHEET_TEMPLATE_NAME}」建立新工作表）"

    closes_at = datetime.now(timezone.utc) + duration
    embed = format_slots_embed(
        slots,
        sheet_name,
        party_id,
        closes_at,
        title=title,
        note=note,
    )

    view = PartySignupView(
        bot,
        sheet_name,
        party_id,
        closes_at,
        title=title,
        note=note,
    )
    message = await interaction.followup.send(embed=embed, view=view)
    view.message = message
    bot.active_views[message.id] = view

    async def auto_close():
        await asyncio.sleep(duration.total_seconds())
        if message.id in bot.active_views:
            await view.close_signup()
            del bot.active_views[message.id]

    view.auto_close_task = asyncio.create_task(auto_close())


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("請在 .env 設定 DISCORD_TOKEN")
    if not GOOGLE_SHEET_ID:
        raise SystemExit("請在 .env 設定 GOOGLE_SHEET_ID")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
