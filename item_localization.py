"""Albion 裝備名稱本地化（資料來源：ao-data/ao-bin-dumps）"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

ITEMS_SOURCE_URL = (
    "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
)
CACHE_PATH = Path(__file__).resolve().parent / "data" / "item_names.json"

LOCALE_API_KEYS: dict[str, str] = {
    "en": "EN-US",
    "zh-TW": "ZH-TW",
    "zh-CN": "ZH-CN",
}

LOCALE_LABELS: dict[str, str] = {
    "en": "English",
    "zh-TW": "繁體中文",
    "zh-CN": "简体中文",
}

TIER_PREFIXES: dict[str, tuple[str, ...]] = {
    "zh-TW": (
        "初學者級",
        "禪師級",
        "宗師級",
        "大師級",
        "專家級",
        "老手級",
        "學徒級",
        "新手級",
    ),
    "zh-CN": (
        "初学者级",
        "禅师级",
        "宗师级",
        "大师级",
        "专家级",
        "老手级",
        "学徒级",
        "新手级",
    ),
}

_load_lock = threading.Lock()
_names: dict[str, dict[str, str]] | None = None


def _fetch_items() -> list[dict]:
    req = urllib.request.Request(ITEMS_SOURCE_URL, headers={"User-Agent": "discordbotalbion/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_cache(items: list[dict]) -> dict[str, dict[str, str]]:
    cache: dict[str, dict[str, str]] = {}
    for item in items:
        unique_name = item.get("UniqueName")
        localized = item.get("LocalizedNames") or {}
        if not unique_name or not localized:
            continue
        cache[unique_name] = {
            key: localized[key]
            for key in ("EN-US", "ZH-TW", "ZH-CN")
            if key in localized
        }
    return cache


def _load_cache_file() -> dict[str, dict[str, str]] | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    items = data.get("items")
    if not isinstance(items, dict):
        return None
    return items


def _save_cache(cache: dict[str, dict[str, str]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({"version": 1, "items": cache}, ensure_ascii=False),
        encoding="utf-8",
    )


def ensure_loaded() -> None:
    global _names
    if _names is not None:
        return
    with _load_lock:
        if _names is not None:
            return
        cached = _load_cache_file()
        if cached:
            _names = cached
            print(f"已載入裝備名稱對照表（{len(_names)} 筆）")
            return
        print("正在下載 Albion 裝備名稱對照表（首次啟動）…")
        try:
            items = _fetch_items()
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            print(f"下載裝備名稱失敗：{exc}")
            _names = {}
            return
        cache = _build_cache(items)
        _save_cache(cache)
        _names = cache
        print(f"已建立裝備名稱對照表（{len(_names)} 筆）")


def parse_power_level(type_id: str) -> int | None:
    match = re.match(r"T(\d)_.*?(?:@(\d))?$", type_id)
    if not match:
        return None
    tier = int(match.group(1))
    enchant = int(match.group(2) or 0)
    return tier + enchant


def get_item_name(type_id: str, locale: str = "en") -> str | None:
    ensure_loaded()
    if not _names:
        return None
    api_key = LOCALE_API_KEYS.get(locale, LOCALE_API_KEYS["en"])
    entry = _names.get(type_id)
    if entry:
        if api_key in entry:
            return entry[api_key]
        if "EN-US" in entry:
            return entry["EN-US"]
    base, _, _enchant = type_id.partition("@")
    if base in _names:
        entry = _names[base]
        return entry.get(api_key) or entry.get("EN-US")
    return None


def format_item_fallback(type_id: str) -> str:
    match = re.match(r"T(\d)_(.+?)(?:@(\d))?$", type_id)
    if not match:
        return type_id
    tier, name, _enchant = match.groups()
    name = name.replace("_", " ").title()
    return name


def _strip_tier_prefix(name: str, locale: str) -> str:
    prefixes = TIER_PREFIXES.get(locale)
    if not prefixes:
        return name
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _with_power_prefix(type_id: str, name: str) -> str:
    power = parse_power_level(type_id)
    if power is None:
        return name
    return f"P{power} {name}"


def format_item_name(type_id: str, locale: str = "en") -> str:
    base, _, _enchant = type_id.partition("@")
    name: str | None = None
    if locale != "en":
        name = get_item_name(type_id, locale) or get_item_name(base, locale)
    if not name:
        name = get_item_name(type_id, "en") or get_item_name(base, "en")
    if not name:
        name = format_item_fallback(type_id)
    if locale in TIER_PREFIXES:
        name = _strip_tier_prefix(name, locale)
    return _with_power_prefix(type_id, name)
