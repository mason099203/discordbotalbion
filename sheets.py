"""Google Sheets 讀寫模組"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

from google.oauth2 import service_account
from googleapiclient.discovery import build

from albion_kill import equipment_bilingual_header

PartyId = Literal["party1", "party2", "party3", "party4"]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

ROLE_EMOJI: dict[str, str] = {
    "caller": "🟡",
    "tank": "🔵",
    "dps": "🔴",
    "support": "🟠",
    "suport": "🟠",
    "healer": "🟢",
    "mount": "⚪",
}

PARTY_RANGES: dict[PartyId, dict[str, str]] = {
    "party1": {"label": "D6:D25", "value": "B6:B25", "role": "C6:C25"},
    "party2": {"label": "L6:L25", "value": "J6:J25", "role": "K6:K25"},
    "party3": {"label": "D34:D53", "value": "B34:B53", "role": "C34:C53"},
    "party4": {"label": "L34:L53", "value": "J34:J53", "role": "K34:K53"},
}

ALL_PARTIES: tuple[PartyId, ...] = ("party1", "party2", "party3", "party4")


@dataclass
class UserSignup:
    party: PartyId
    slot: Slot


@dataclass
class Slot:
    index: int
    role: str
    build: str
    value: str
    label: str
    label_cell: str
    value_cell: str

    @property
    def display_value(self) -> str:
        return signup_display(self.value)

    @property
    def user_id(self) -> int | None:
        return parse_signup(self.value)[1]


def parse_signup(value: str) -> tuple[str, int | None]:
    value = value.strip()
    if not value:
        return "", None
    if "|" in value:
        name, uid_str = value.rsplit("|", 1)
        if uid_str.isdigit():
            return name.strip(), int(uid_str)
    return value, None


def signup_display(value: str) -> str:
    name, uid = parse_signup(value)
    if not name and uid is None:
        return "(空)"
    if uid:
        return f"<@{uid}>"
    return name or "(空)"


def signup_owned_by(value: str, user_id: int, display_name: str) -> bool:
    name, uid = parse_signup(value)
    if uid == user_id:
        return True
    return name == display_name


def load_service_account_credentials(
    credentials_path: str | None = None,
) -> service_account.Credentials:
    json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_str:
        info = json.loads(json_str)
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )

    path = credentials_path or os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "請在 .env 設定 GOOGLE_SERVICE_ACCOUNT_JSON，"
            "或提供 GOOGLE_CREDENTIALS 檔案路徑"
        )
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


class GoogleSheetClient:
    def __init__(self, spreadsheet_id: str, credentials_path: str | None = None):
        creds = load_service_account_credentials(credentials_path)
        # cache_discovery=False：避免 discovery 文件常駐記憶體
        self._service = build(
            "sheets", "v4", credentials=creds, cache_discovery=False
        )
        self.spreadsheet_id = spreadsheet_id

    def _col_row_to_a1(self, col: str, row: int) -> str:
        return f"{col}{row}"

    def _parse_range_start(self, cell_range: str) -> tuple[str, int]:
        col = "".join(c for c in cell_range.split(":")[0] if c.isalpha())
        row = int("".join(c for c in cell_range.split(":")[0] if c.isdigit()))
        return col, row

    def _cell_value(self, rows: list, index: int) -> str:
        if index < len(rows) and rows[index]:
            return str(rows[index][0]).strip()
        return ""

    def _clean_label(self, text: str) -> str:
        return text.replace("禪師級", "")

    def _format_role(self, role: str) -> str:
        role = role.strip()
        if not role:
            return ""
        emoji = ROLE_EMOJI.get(role.lower(), "")
        return f"{emoji} {role}" if emoji else role

    def read_party_slots(self, sheet_name: str, party: PartyId) -> list[Slot]:
        ranges = PARTY_RANGES[party]
        _, start_row = self._parse_range_start(ranges["label"])
        value_col, _ = self._parse_range_start(ranges["value"])

        result = (
            self._service.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=[
                    f"'{sheet_name}'!{ranges['label']}",
                    f"'{sheet_name}'!{ranges['value']}",
                    f"'{sheet_name}'!{ranges['role']}",
                ],
            )
            .execute()
        )

        label_rows = result["valueRanges"][0].get("values", [])
        value_rows = result["valueRanges"][1].get("values", [])
        role_rows = result["valueRanges"][2].get("values", [])

        slots: list[Slot] = []
        for i in range(20):
            row = start_row + i
            build = self._clean_label(self._cell_value(label_rows, i))
            if not build:
                continue

            role_raw = self._cell_value(role_rows, i)
            role = self._format_role(role_raw)
            value = self._cell_value(value_rows, i)
            label = self._clean_label(f"{role} · {build}" if role else build)

            slots.append(
                Slot(
                    index=i,
                    role=role,
                    build=build,
                    value=value,
                    label=label,
                    label_cell=self._col_row_to_a1(
                        self._parse_range_start(ranges["label"])[0], row
                    ),
                    value_cell=self._col_row_to_a1(value_col, row),
                )
            )
        return slots

    def find_user_signup(
        self, sheet_name: str, user_id: int, display_name: str
    ) -> UserSignup | None:
        for party in ALL_PARTIES:
            for slot in self.read_party_slots(sheet_name, party):
                if signup_owned_by(slot.value, user_id, display_name):
                    return UserSignup(party=party, slot=slot)
        return None

    def write_slot_value(
        self, sheet_name: str, value_cell: str, username: str
    ) -> None:
        self._service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_name}'!{value_cell}",
            valueInputOption="USER_ENTERED",
            body={"values": [[username]]},
        ).execute()

    def clear_slot_value(self, sheet_name: str, value_cell: str) -> None:
        self.write_slot_value(sheet_name, value_cell, "")

    def _get_sheet_properties(self) -> list[dict]:
        meta = (
            self._service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id)
            .execute()
        )
        return [s["properties"] for s in meta.get("sheets", [])]

    def sheet_exists(self, sheet_name: str) -> bool:
        return any(p["title"] == sheet_name for p in self._get_sheet_properties())

    def _get_sheet_id_by_name(self, sheet_name: str) -> int | None:
        for props in self._get_sheet_properties():
            if props["title"] == sheet_name:
                return props["sheetId"]
        return None

    def ensure_sheet_from_template(
        self, sheet_name: str, template_name: str
    ) -> bool:
        """若工作表不存在，從範本複製建立。回傳是否新建。"""
        if self.sheet_exists(sheet_name):
            return False

        template_id = self._get_sheet_id_by_name(template_name)
        if template_id is None:
            raise ValueError(f"找不到範本工作表「{template_name}」")

        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "duplicateSheet": {
                            "sourceSheetId": template_id,
                            "newSheetName": sheet_name,
                        }
                    }
                ]
            },
        ).execute()
        return True

    def delete_sheet_tab(
        self, sheet_name: str, protected_names: set[str] | None = None
    ) -> None:
        protected = protected_names or set()
        if sheet_name in protected:
            raise ValueError(f"無法刪除受保護的工作表「{sheet_name}」")

        sheet_id = self._get_sheet_id_by_name(sheet_name)
        if sheet_id is None:
            return

        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]},
        ).execute()


BINDINGS_SHEET = "bindings"

BINDINGS_HEADER = [
    "Discord User ID",
    "Discord 顯示名",
    "遊戲名稱",
    "更新時間",
]

REGEAR_HEADER = [
    "申請時間",
    "Discord User ID",
    "Discord 名",
    "遊戲名",
    "Kill ID",
    "死者",
    "擊殺者",
    "死者IP",
    "擊殺者IP",
    *equipment_bilingual_header(),
    "Fame",
    "區域",
    "死亡時間",
    "官方連結",
]


class RegearSheetClient(GoogleSheetClient):
    """Regear 試算表：bindings + 月份分頁。"""

    def ensure_bindings_sheet(self) -> None:
        if self.sheet_exists(BINDINGS_SHEET):
            return
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {"title": BINDINGS_SHEET},
                        }
                    }
                ]
            },
        ).execute()
        self._service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{BINDINGS_SHEET}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [BINDINGS_HEADER]},
        ).execute()

    def ensure_month_sheet(self, sheet_name: str) -> None:
        if not self.sheet_exists(sheet_name):
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {"title": sheet_name},
                            }
                        }
                    ]
                },
            ).execute()
        # 新建或既有分頁都寫入最新表頭（含各部位英文／中文欄）
        self._service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [REGEAR_HEADER]},
        ).execute()

    def get_binding(self, discord_user_id: int) -> str | None:
        self.ensure_bindings_sheet()
        result = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{BINDINGS_SHEET}'!A:C",
            )
            .execute()
        )
        rows = result.get("values") or []
        uid = str(discord_user_id)
        for row in rows[1:]:
            if not row:
                continue
            if str(row[0]).strip() == uid and len(row) >= 3 and row[2].strip():
                return row[2].strip()
        return None

    def get_discord_ids_for_game_name(self, game_name: str) -> list[int]:
        self.ensure_bindings_sheet()
        result = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{BINDINGS_SHEET}'!A:C",
            )
            .execute()
        )
        rows = result.get("values") or []
        target = game_name.strip().casefold()
        found: list[int] = []
        for row in rows[1:]:
            if len(row) < 3:
                continue
            if str(row[2]).strip().casefold() != target:
                continue
            uid_str = str(row[0]).strip()
            if uid_str.isdigit():
                found.append(int(uid_str))
        return found

    def upsert_binding(
        self, discord_user_id: int, discord_display: str, game_name: str, updated_at: str
    ) -> None:
        self.ensure_bindings_sheet()
        result = (
            self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{BINDINGS_SHEET}'!A:A",
            )
            .execute()
        )
        rows = result.get("values") or []
        uid = str(discord_user_id)
        row_index: int | None = None
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if row and str(row[0]).strip() == uid:
                row_index = i + 1
                break

        values = [[uid, discord_display, game_name.strip(), updated_at]]
        if row_index is not None:
            self._service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{BINDINGS_SHEET}'!A{row_index}:D{row_index}",
                valueInputOption="USER_ENTERED",
                body={"values": values},
            ).execute()
        else:
            self._service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{BINDINGS_SHEET}'!A:D",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()

    def kill_id_exists(self, kill_id: int, sheet_names: list[str]) -> bool:
        target = str(kill_id)
        for sheet_name in sheet_names:
            if not self.sheet_exists(sheet_name):
                continue
            result = (
                self._service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{sheet_name}'!E:E",
                )
                .execute()
            )
            for row in result.get("values") or []:
                if row and str(row[0]).strip() == target:
                    return True
        return False

    def append_regear_row(self, sheet_name: str, row: list[str]) -> None:
        self.ensure_month_sheet(sheet_name)
        self._service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_name}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
