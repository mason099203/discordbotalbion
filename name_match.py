"""Discord 顯示名／暱稱與 Albion 遊戲名比對"""

from __future__ import annotations

import re

import discord


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def name_matches(candidate: str, game_name: str) -> bool:
    """候選字串是否包含或等於遊戲名（不區分大小寫）。"""
    c = normalize_name(candidate)
    g = normalize_name(game_name)
    if not c or not g:
        return False
    if c == g:
        return True
    # 常見暱稱格式：遊戲名 | 職位、[公會] 遊戲名
    parts = re.split(r"[|/\-–—·•]", c)
    if any(normalize_name(p) == g for p in parts if p.strip()):
        return True
    return g in c


def member_display_names(member: discord.Member) -> list[str]:
    names = [member.display_name, member.name]
    if member.nick:
        names.append(member.nick)
    if member.global_name:
        names.append(member.global_name)
    return names


def member_matches_game_name(member: discord.Member, game_name: str) -> bool:
    return any(name_matches(n, game_name) for n in member_display_names(member))


async def find_members_by_game_name(
    guild: discord.Guild, game_name: str
) -> list[discord.Member]:
    """依暱稱／顯示名尋找成員（需 Members Intent）。

    優先用已快取成員；大型伺服器避免強制 chunk 整服以省記憶體。
    """
    if not guild.chunked and len(guild.members) < 200:
        try:
            await guild.chunk(cache=True)
        except discord.HTTPException:
            pass

    matched: list[discord.Member] = []
    for member in guild.members:
        if member.bot:
            continue
        if member_matches_game_name(member, game_name):
            matched.append(member)
    return matched


def resolve_applicant_game_name(
    member: discord.Member, bound_name: str | None
) -> str | None:
    """優先使用綁定名稱；否則回傳顯示名供比對。"""
    if bound_name and bound_name.strip():
        return bound_name.strip()
    display = member.display_name.strip()
    return display or None
