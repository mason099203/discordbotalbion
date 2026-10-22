"""Albion 裝備名稱本地化（SQLite 每次查詢短連線＋全域鎖，避免並發／跨執行緒損壞）"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

ITEMS_SOURCE_URL = (
    "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
)
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "item_names.db"
LEGACY_JSON_PATH = DATA_DIR / "item_names.json"

STORED_LOCALES: tuple[str, ...] = ("EN-US", "ZH-TW", "ZH-CN")

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

_LRU_MAX = 256
_lru: OrderedDict[tuple[str, str], str | None] = OrderedDict()
_lru_lock = threading.Lock()
_db_lock = threading.Lock()
_db_ready = False


def _open_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            unique_name TEXT PRIMARY KEY,
            en_us TEXT,
            zh_tw TEXT,
            zh_cn TEXT
        )
        """
    )
    conn.commit()


def _fetch_items() -> list[dict]:
    req = urllib.request.Request(
        ITEMS_SOURCE_URL, headers={"User-Agent": "discordbotalbion/1.0"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _row_from_localized(unique_name: str, localized: dict) -> tuple[str, str, str, str]:
    return (
        unique_name,
        localized.get("EN-US") or "",
        localized.get("ZH-TW") or "",
        localized.get("ZH-CN") or "",
    )


def _insert_batches(
    conn: sqlite3.Connection, rows: list[tuple[str, str, str, str]]
) -> int:
    count = 0
    batch: list[tuple[str, str, str, str]] = []
    sql = (
        "INSERT OR REPLACE INTO items(unique_name, en_us, zh_tw, zh_cn) "
        "VALUES (?, ?, ?, ?)"
    )
    for row in rows:
        batch.append(row)
        if len(batch) >= 500:
            conn.executemany(sql, batch)
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        count += len(batch)
    conn.commit()
    return count


def _populate_from_remote(conn: sqlite3.Connection) -> int:
    print("正在下載 Albion 裝備名稱對照表（首次／重建）…")
    items = _fetch_items()
    try:
        conn.execute("DELETE FROM items")
        rows = [
            _row_from_localized(item["UniqueName"], item.get("LocalizedNames") or {})
            for item in items
            if item.get("UniqueName")
            and item.get("LocalizedNames")
            and any((item.get("LocalizedNames") or {}).get(k) for k in STORED_LOCALES)
        ]
        return _insert_batches(conn, rows)
    finally:
        del items


def _migrate_from_json(conn: sqlite3.Connection) -> int | None:
    if not LEGACY_JSON_PATH.exists():
        return None
    try:
        data = json.loads(LEGACY_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    items = data.get("items")
    if not isinstance(items, dict):
        return None
    try:
        conn.execute("DELETE FROM items")
        rows = [
            _row_from_localized(str(name), loc)
            for name, loc in items.items()
            if isinstance(loc, dict)
        ]
        count = _insert_batches(conn, rows)
        try:
            LEGACY_JSON_PATH.unlink()
            print("已刪除舊版 item_names.json，改用 SQLite")
        except OSError:
            pass
        return count
    finally:
        del data, items


def ensure_loaded() -> None:
    global _db_ready
    with _db_lock:
        if _db_ready:
            return
        conn = _open_conn()
        try:
            _init_schema(conn)
            row = conn.execute("SELECT COUNT(*) FROM items").fetchone()
            count = int(row[0]) if row else 0
            if count == 0:
                migrated = _migrate_from_json(conn)
                if migrated is not None:
                    count = migrated
                    print(f"已從 JSON 匯入裝備名稱（{count} 筆）→ SQLite")
                else:
                    try:
                        count = _populate_from_remote(conn)
                        print(f"已建立裝備名稱 SQLite（{count} 筆）")
                    except (
                        urllib.error.URLError,
                        json.JSONDecodeError,
                        TimeoutError,
                        MemoryError,
                    ) as exc:
                        print(f"下載裝備名稱失敗：{exc}")
                        return
            else:
                print(f"裝備名稱 SQLite 已就緒（{count} 筆，按需查詢）")
            _db_ready = True
        except sqlite3.Error as exc:
            print(f"SQLite 初始化失敗：{exc}")
            _db_ready = False
        finally:
            conn.close()


def _lru_get(key: tuple[str, str]) -> tuple[bool, str | None]:
    with _lru_lock:
        if key not in _lru:
            return False, None
        _lru.move_to_end(key)
        return True, _lru[key]


def _lru_put(key: tuple[str, str], value: str | None) -> None:
    with _lru_lock:
        _lru[key] = value
        _lru.move_to_end(key)
        while len(_lru) > _LRU_MAX:
            _lru.popitem(last=False)


def _query_name(unique_name: str, api_key: str) -> str | None:
    ensure_loaded()
    if not _db_ready:
        return None
    col = {"EN-US": "en_us", "ZH-TW": "zh_tw", "ZH-CN": "zh_cn"}.get(api_key, "en_us")
    with _db_lock:
        conn = _open_conn()
        try:
            row = conn.execute(
                f"SELECT {col}, en_us FROM items WHERE unique_name = ?",
                (unique_name,),
            ).fetchone()
        except sqlite3.Error as exc:
            print(f"SQLite 查詢失敗：{exc}")
            return None
        finally:
            conn.close()
    if not row:
        return None
    preferred, en_us = row[0] or "", row[1] or ""
    return preferred or en_us or None


def parse_power_level(type_id: str) -> int | None:
    match = re.match(r"T(\d)_.*?(?:@(\d))?$", type_id)
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2) or 0)


def get_item_name(type_id: str, locale: str = "en") -> str | None:
    api_key = LOCALE_API_KEYS.get(locale, LOCALE_API_KEYS["en"])
    cache_key = (type_id, api_key)
    hit, cached = _lru_get(cache_key)
    if hit:
        return cached

    name = _query_name(type_id, api_key)
    if name is None:
        base, _, _ = type_id.partition("@")
        if base != type_id:
            name = _query_name(base, api_key)
    _lru_put(cache_key, name)
    return name


def format_item_fallback(type_id: str) -> str:
    match = re.match(r"T(\d)_(.+?)(?:@(\d))?$", type_id)
    if not match:
        return type_id
    return match.group(2).replace("_", " ").title()


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
    return f"P{power} {name}" if power is not None else name


def format_item_tier(type_id: str) -> str:
    match = re.match(r"T(\d)_.*?(?:@(\d))?$", type_id)
    if not match:
        return ""
    return f"T{match.group(1)}.{match.group(2) or '0'}"


def format_item_name(type_id: str, locale: str = "en", *, with_power: bool = True) -> str:
    base, _, _ = type_id.partition("@")
    name: str | None = None
    if locale != "en":
        name = get_item_name(type_id, locale) or get_item_name(base, locale)
    if not name:
        name = get_item_name(type_id, "en") or get_item_name(base, "en")
    if not name:
        name = format_item_fallback(type_id)
    if locale in TIER_PREFIXES:
        name = _strip_tier_prefix(name, locale)
    if with_power:
        return _with_power_prefix(type_id, name)
    return name
