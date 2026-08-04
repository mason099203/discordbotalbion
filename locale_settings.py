"""伺服器語言設定"""

from __future__ import annotations

import os

from item_localization import LOCALE_LABELS

DEFAULT_LOCALE = os.getenv("ITEM_LOCALE", "zh-TW").strip() or "zh-TW"
SUPPORTED_LOCALES = frozenset(LOCALE_LABELS)


def normalize_locale(locale: str) -> str:
    locale = locale.strip()
    if locale in SUPPORTED_LOCALES:
        return locale
    raise ValueError(f"不支援的語言：{locale}")


def locale_label(locale: str) -> str:
    return LOCALE_LABELS.get(locale, locale)
