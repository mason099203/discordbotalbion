"""Albion 擊殺板 API"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from display_settings import DisplaySettings
from item_localization import format_item_name

ALBION_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?albiononline\.com(?:/([a-z]{2}))?/killboard/kill/(\d+)",
    re.IGNORECASE,
)
KILLBOARD1_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?killboard-1\.com(?:/([a-z]{2}))?/event/(\d+)",
    re.IGNORECASE,
)
DEATH_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:albiononline\.com(?:/[a-z]{2})?/killboard/kill/\d+|killboard-1\.com(?:/[a-z]{2})?/event/\d+)",
    re.IGNORECASE,
)


def to_official_kill_url(kill_id: int, locale: str | None = None) -> str:
    if locale:
        return f"https://albiononline.com/{locale.lower()}/killboard/kill/{kill_id}"
    return f"https://albiononline.com/killboard/kill/{kill_id}"


def parse_death_url(url: str) -> DeathLink | None:
    match = ALBION_URL_PATTERN.search(url)
    if match:
        locale, kill_id = match.group(1), int(match.group(2))
        return DeathLink(kill_id, to_official_kill_url(kill_id, locale))

    match = KILLBOARD1_URL_PATTERN.search(url)
    if match:
        locale, kill_id = match.group(1), int(match.group(2))
        return DeathLink(kill_id, to_official_kill_url(kill_id, locale))

    return None


def extract_kill_id(url: str) -> int | None:
    link = parse_death_url(url)
    return link.kill_id if link else None


API_BASES = (
    "https://gameinfo.albiononline.com/api/gameinfo",
    "https://gameinfo-ams.albiononline.com/api/gameinfo",
    "https://gameinfo-sgp.albiononline.com/api/gameinfo",
)

KILL_AREA_LABELS = {
    "OPEN_WORLD": "開放世界",
    "OPEN_WORLD_RED": "紅區",
    "OPEN_WORLD_BLACK": "黑區",
    "OPEN_WORLD_YELLOW": "黃區",
    "HELLGATE": "地獄之門",
    "CORRUPTED_DUNGEON": "腐化地城",
    "MISTS": "迷霧",
    "AVALON": "阿瓦隆",
}

EQUIPMENT_SLOTS: tuple[tuple[str, str], ...] = (
    ("MainHand", "主手"),
    ("OffHand", "副手"),
    ("Head", "頭"),
    ("Armor", "身"),
    ("Shoes", "鞋"),
    ("Cape", "披風"),
    # ("Bag", "包"),
    ("Mount", "坐騎"),
    # ("Potion", "藥水"),
    # ("Food", "食物"),
)


@dataclass
class DeathLink:
    kill_id: int
    official_url: str


@dataclass
class KillInfo:
    kill_id: int
    victim_name: str
    victim_guild: str
    victim_alliance: str
    victim_ip: float
    victim_equipment: str
    killer_name: str
    killer_guild: str
    killer_alliance: str
    killer_ip: float
    killer_equipment: str
    death_fame: int
    kill_area: str
    timestamp: datetime | None
    participants: int


def format_item(type_id: str, locale: str = "en") -> str:
    return format_item_name(type_id, locale)


def format_equipment(equipment: dict | None, locale: str = "en") -> str:
    if not equipment:
        return ""
    lines: list[str] = []
    for slot_key, slot_label in EQUIPMENT_SLOTS:
        item = equipment.get(slot_key)
        if not item or not item.get("Type"):
            continue
        name = format_item(item["Type"], locale)
        count = int(item.get("Count") or 1)
        if count > 1:
            name = f"{name} x{count}"
        lines.append(f"{slot_label}：{name}")
    return "\n".join(lines)


def _guild_line(name: str, guild: str, alliance: str) -> str:
    parts = [name]
    if guild:
        if alliance:
            parts.append(f"[{alliance}] {guild}")
        else:
            parts.append(guild)
    return " · ".join(parts)


def _fetch_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "discordbotalbion/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _parse_kill(data: dict, kill_id: int, locale: str = "en") -> KillInfo:
    victim = data.get("Victim") or {}
    killer = data.get("Killer") or {}

    ts_raw = data.get("TimeStamp")
    timestamp = None
    if ts_raw:
        try:
            ts_clean = re.sub(r"\.\d+", "", ts_raw.replace("Z", "+00:00"))
            timestamp = datetime.fromisoformat(ts_clean)
        except ValueError:
            timestamp = None

    area_key = data.get("KillArea") or ""
    kill_area = KILL_AREA_LABELS.get(area_key, area_key or "未知")

    return KillInfo(
        kill_id=kill_id,
        victim_name=victim.get("Name") or "未知",
        victim_guild=victim.get("GuildName") or "",
        victim_alliance=victim.get("AllianceName") or "",
        victim_ip=float(victim.get("AverageItemPower") or 0),
        victim_equipment=format_equipment(victim.get("Equipment"), locale),
        killer_name=killer.get("Name") or "未知",
        killer_guild=killer.get("GuildName") or "",
        killer_alliance=killer.get("AllianceName") or "",
        killer_ip=float(killer.get("AverageItemPower") or 0),
        killer_equipment=format_equipment(killer.get("Equipment"), locale),
        death_fame=int(victim.get("DeathFame") or data.get("TotalVictimKillFame") or 0),
        kill_area=kill_area,
        timestamp=timestamp,
        participants=int(data.get("numberOfParticipants") or 0),
    )


async def fetch_kill_info(kill_id: int, locale: str = "en") -> KillInfo | None:
    for base in API_BASES:
        url = f"{base}/events/{kill_id}"
        data = await asyncio.to_thread(_fetch_json, url)
        if data and data.get("Victim"):
            return _parse_kill(data, kill_id, locale)
    return None


def format_kill_message(
    info: KillInfo, url: str, settings: DisplaySettings | None = None
) -> str:
    settings = settings or DisplaySettings.default()
    victim = _guild_line(info.victim_name, info.victim_guild, info.victim_alliance)
    killer = _guild_line(info.killer_name, info.killer_guild, info.killer_alliance)
    lines: list[str] = []

    if settings.victim:
        lines.append(f"💀 **{victim}**")
    if settings.victim_ip:
        lines.append(f"　IP {info.victim_ip:.0f}")
    if settings.victim_equipment and info.victim_equipment:
        lines.append("death：")
        for row in info.victim_equipment.split("\n"):
            lines.append(f"　{row}")

    if settings.killer:
        lines.append(f"擊殺：**{killer}**")
    if settings.killer_ip:
        lines.append(f"IP {info.killer_ip:.0f}")
    if settings.killer_equipment and info.killer_equipment:
        lines.append("🎒 killer：")
        for row in info.killer_equipment.split("\n"):
            lines.append(f"　{row}")

    if settings.death_fame:
        lines.append(f"價值：{info.death_fame:,} Fame")
    if settings.kill_area:
        lines.append(f"area：{info.kill_area}")
    if settings.participants and info.participants > 0:
        lines.append(f"👥 參與人數：{info.participants}")
    if settings.kill_time and info.timestamp:
        ts = int(info.timestamp.timestamp())
        lines.append(f"死亡時間：<t:{ts}:F>")
    if settings.official_url:
        lines.append(url)

    return "\n".join(lines) if lines else url
