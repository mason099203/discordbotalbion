"""Discord /createparty 報名表單功能"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol

import discord
from discord import app_commands

from sheets import (
    GoogleSheetClient,
    PartyId,
    Slot,
    parse_signup,
    signup_owned_by,
)

SHEET_TEMPLATE_NAME = os.getenv("SHEET_TEMPLATE_NAME", "範本")

PARTY_LABELS = {
    "party1": "Party 1",
    "party2": "Party 2",
    "party3": "Party 3",
    "party4": "Party 4",
}

SignupUserIds = dict[tuple[str, str], int]


class PartyHost(Protocol):
    sheets: GoogleSheetClient
    active_views: dict[int, "PartySignupView"]
    signup_user_ids: SignupUserIds

    def clear_signup_user_ids(self, sheet_name: str) -> None: ...

    async def refresh_sheet_views(self, sheet_name: str) -> None: ...


def get_text_channel(
    channel: discord.abc.GuildChannel | discord.Thread | None,
) -> discord.TextChannel | None:
    if isinstance(channel, discord.TextChannel):
        return channel
    if isinstance(channel, discord.Thread):
        return channel.parent
    return None


async def create_log_thread(
    interaction: discord.Interaction,
    sheet_name: str,
    party_id: PartyId,
) -> discord.Thread | None:
    text_channel = get_text_channel(interaction.channel)
    if text_channel is None:
        return None

    thread_name = f"報名紀錄-{PARTY_LABELS[party_id]}-{sheet_name}"
    if len(thread_name) > 100:
        thread_name = thread_name[:97] + "..."

    try:
        thread = await text_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            auto_archive_duration=10080,
        )
    except (discord.Forbidden, discord.HTTPException):
        return None

    if isinstance(interaction.user, discord.Member):
        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            pass
    return thread


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


def slot_display_value(
    slot: Slot,
    sheet_name: str,
    signup_user_ids: SignupUserIds,
) -> str:
    if not slot.value.strip():
        return "(空)"
    uid = signup_user_ids.get((sheet_name, slot.value_cell))
    if uid:
        return f"<@{uid}>"
    _, parsed_uid = parse_signup(slot.value)
    if parsed_uid:
        return f"<@{parsed_uid}>"
    name, _ = parse_signup(slot.value)
    return name or "(空)"


def format_slots_table(
    slots: list[Slot],
    sheet_name: str,
    signup_user_ids: SignupUserIds,
) -> str:
    lines = ["職位\tBuild\t報名者"]
    for s in slots:
        role = s.role or "-"
        lines.append(
            f"{role}\t{s.build}\t{slot_display_value(s, sheet_name, signup_user_ids)}"
        )
    return "\n".join(lines)


def format_slots_embed(
    slots: list[Slot],
    sheet_name: str,
    party: PartyId,
    closes_at: datetime | None,
    signup_user_ids: SignupUserIds,
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

    table = format_slots_table(slots, sheet_name, signup_user_ids) if slots else "無資料"
    embed.add_field(name="名額", value=table, inline=False)

    if closed:
        embed.set_footer(text="報名已關閉")
    elif closes_at:
        ts = int(closes_at.timestamp())
        embed.set_footer(text="同一工作表每人限報一個位置（跨 Party）；重選將自動取消原報名")
        embed.add_field(
            name="關閉時間",
            value=f"<t:{ts}:F> (<t:{ts}:R>)",
            inline=False,
        )
    else:
        embed.set_footer(text="同一工作表每人限報一個位置（跨 Party）；重選將自動取消原報名")

    return embed


class DeleteConfirmView(discord.ui.View):
    def __init__(self, party_view: "PartySignupView"):
        super().__init__(timeout=60)
        self.party_view = party_view

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="確認刪除", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(view=self)
        await self.party_view._execute_delete(interaction)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="已取消刪除。", view=self)


class PartySignupView(discord.ui.View):
    def __init__(
        self,
        bot: PartyHost,
        sheet_name: str,
        party: PartyId,
        closes_at: datetime | None,
        title: str | None = None,
        note: str | None = None,
        message: discord.Message | None = None,
        log_thread: discord.Thread | None = None,
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.sheet_name = sheet_name
        self.party = party
        self.closes_at = closes_at
        self.title = title
        self.note = note
        self.message = message
        self.log_thread = log_thread
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
                    placeholder="取消自己的報名",
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

    async def _log_activity(self, content: str) -> None:
        if not self.log_thread:
            return
        try:
            await self.log_thread.send(content)
        except discord.HTTPException:
            pass

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
                f"**{slot.label}** 已被 "
                f"{slot_display_value(slot, self.sheet_name, self.bot.signup_user_ids)} 報名。",
                ephemeral=True,
            )
            return

        user = interaction.user
        existing_signup = self.bot.sheets.find_user_signup(
            self.sheet_name, user.id, user.display_name
        )
        if existing_signup:
            self.bot.signup_user_ids.pop(
                (self.sheet_name, existing_signup.slot.value_cell), None
            )
            self.bot.sheets.clear_slot_value(
                self.sheet_name, existing_signup.slot.value_cell
            )

        self.bot.sheets.write_slot_value(
            self.sheet_name,
            slot.value_cell,
            user.display_name,
        )
        self.bot.signup_user_ids[(self.sheet_name, slot.value_cell)] = user.id

        if existing_signup:
            existing = existing_signup.slot
            existing_party = existing_signup.party
            if existing_party != self.party:
                await interaction.response.send_message(
                    f"已取消 {PARTY_LABELS[existing_party]} **{existing.label}** 的報名，"
                    f"改報 **{slot.label}**！",
                    ephemeral=True,
                )
                await self._log_activity(
                    f"🔄 {user.mention} 從 {PARTY_LABELS[existing_party]} **{existing.label}** "
                    f"改報 {PARTY_LABELS[self.party]} **{slot.label}**"
                )
            elif existing.index != index:
                await interaction.response.send_message(
                    f"已取消 **{existing.label}** 的報名，改報 **{slot.label}**！",
                    ephemeral=True,
                )
                await self._log_activity(
                    f"🔄 {user.mention} 從 **{existing.label}** 改報 **{slot.label}**"
                )
            else:
                await interaction.response.send_message(
                    f"已成功報名 **{slot.label}**！", ephemeral=True
                )
                await self._log_activity(f"✅ {user.mention} 報名 **{slot.label}**")
        else:
            await interaction.response.send_message(
                f"已成功報名 **{slot.label}**！", ephemeral=True
            )
            await self._log_activity(f"✅ {user.mention} 報名 **{slot.label}**")
        await self.bot.refresh_sheet_views(self.sheet_name)

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

        if not signup_owned_by(
            slot.value, interaction.user.id, interaction.user.display_name
        ):
            await interaction.response.send_message(
                "只能取消自己的報名。", ephemeral=True
            )
            return

        cancelled_display = slot_display_value(
            slot, self.sheet_name, self.bot.signup_user_ids
        )
        self.bot.sheets.clear_slot_value(self.sheet_name, slot.value_cell)
        self.bot.signup_user_ids.pop((self.sheet_name, slot.value_cell), None)
        if signup_owned_by(
            slot.value, interaction.user.id, interaction.user.display_name
        ):
            msg = f"已取消 **{slot.label}** 的報名。"
            await self._log_activity(
                f"❌ {interaction.user.mention} 取消 **{slot.label}** 的報名"
            )
        else:
            msg = f"已取消 **{slot.label}**（{cancelled_display}）的報名。"
            await self._log_activity(
                f"❌ {interaction.user.mention} 取消 {cancelled_display} 的 **{slot.label}** 報名"
            )
        await interaction.response.send_message(msg, ephemeral=True)
        await self.bot.refresh_sheet_views(self.sheet_name)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        slots = self.bot.sheets.read_party_slots(self.sheet_name, self.party)
        self._build_select()
        embed = format_slots_embed(
            slots,
            self.sheet_name,
            self.party,
            self.closes_at,
            self.bot.signup_user_ids,
            self.closed,
            self.title,
            self.note,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.message = interaction.message

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"⚠️ 確定要刪除 **{PARTY_LABELS[self.party]}** 報名表單？\n"
            f"工作表：`{self.sheet_name}`\n\n"
            "此操作無法復原，對應的工作表分頁也會一併刪除。",
            ephemeral=True,
            view=DeleteConfirmView(self),
        )

    async def _execute_delete(self, interaction: discord.Interaction) -> None:
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

        self.bot.clear_signup_user_ids(self.sheet_name)

        if interaction.message and self.message:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass

        await self._log_activity(
            f"🗑️ {interaction.user.mention} 刪除報名表單"
        )

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
            self.bot.signup_user_ids,
            self.closed,
            self.title,
            self.note,
        )
        await self.message.edit(embed=embed, view=self)

    async def close_signup(self) -> None:
        self.closed = True
        await self._log_activity("🔒 報名已關閉")
        await self.refresh_message()


def register_createparty(bot: discord.Client) -> None:
    """在既有 Bot 的 CommandTree 註冊 /createparty。"""

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
        host: PartyHost = bot  # type: ignore[assignment]

        sheet_created = False
        try:
            sheet_created = host.sheets.ensure_sheet_from_template(
                sheet_name, SHEET_TEMPLATE_NAME
            )
            slots = host.sheets.read_party_slots(sheet_name, party_id)
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

        log_thread = await create_log_thread(interaction, sheet_name, party_id)
        if log_thread:
            thread_note = f"📜 動態紀錄：<#{log_thread.id}>"
            note = f"{note}\n{thread_note}" if note else thread_note

        closes_at = datetime.now(timezone.utc) + duration
        embed = format_slots_embed(
            slots,
            sheet_name,
            party_id,
            closes_at,
            host.signup_user_ids,
            title=title,
            note=note,
        )

        view = PartySignupView(
            host,
            sheet_name,
            party_id,
            closes_at,
            title=title,
            note=note,
            log_thread=log_thread,
        )
        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message
        host.active_views[message.id] = view

        if log_thread:
            ts = int(closes_at.timestamp())
            await view._log_activity(
                f"📋 {interaction.user.mention} 建立報名表單\n"
                f"工作表：`{sheet_name}` · {PARTY_LABELS[party_id]}\n"
                f"關閉時間：<t:{ts}:F> (<t:{ts}:R>)"
            )
        elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "⚠️ 無法建立私人討論串（請確認 Bot 有「建立私人討論串」權限），動態紀錄將不會顯示。",
                ephemeral=True,
            )

        async def auto_close():
            await asyncio.sleep(duration.total_seconds())
            if message.id in host.active_views:
                await view.close_signup()
                del host.active_views[message.id]

        view.auto_close_task = asyncio.create_task(auto_close())
