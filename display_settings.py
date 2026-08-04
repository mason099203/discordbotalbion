"""死亡資訊顯示欄位設定"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Iterator

FIELD_LABELS: dict[str, str] = {
    "victim": "死者（名稱／公會）",
    "victim_ip": "死者 IP",
    "victim_equipment": "死者裝備",
    "killer": "擊殺者（名稱／公會）",
    "killer_ip": "擊殺者 IP",
    "killer_equipment": "擊殺者裝備",
    "death_fame": "死亡價值",
    "kill_area": "區域",
    "participants": "參與人數",
    "kill_time": "死亡時間",
    "official_url": "官方連結",
}


@dataclass
class DisplaySettings:
    victim: bool = True
    victim_ip: bool = True
    victim_equipment: bool = True
    killer: bool = True
    killer_ip: bool = True
    killer_equipment: bool = True
    death_fame: bool = True
    kill_area: bool = True
    participants: bool = True
    kill_time: bool = False
    official_url: bool = True

    @classmethod
    def default(cls) -> DisplaySettings:
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> DisplaySettings:
        data = dict(data)
        if "victim_weapon" in data and "victim_equipment" not in data:
            data["victim_equipment"] = data.pop("victim_weapon")
        if "killer_weapon" in data and "killer_equipment" not in data:
            data["killer_equipment"] = data.pop("killer_weapon")
        defaults = cls.default()
        kwargs = {}
        for f in fields(cls):
            if f.name in data:
                kwargs[f.name] = bool(data[f.name])
        return cls(**{**defaults.__dict__, **kwargs})

    def to_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def toggle(self, field: str) -> None:
        if field not in FIELD_LABELS:
            raise KeyError(field)
        setattr(self, field, not getattr(self, field))

    def set_field(self, field: str, enabled: bool) -> None:
        if field not in FIELD_LABELS:
            raise KeyError(field)
        setattr(self, field, enabled)

    def iter_fields(self) -> Iterator[tuple[str, str, bool]]:
        for key, label in FIELD_LABELS.items():
            yield key, label, getattr(self, key)

    def format_summary(self) -> str:
        lines = ["**死亡資訊顯示設定**", ""]
        for _, label, enabled in self.iter_fields():
            mark = "✅" if enabled else "❌"
            lines.append(f"{mark} {label}")
        lines.append("")
        lines.append("使用 `/settings set` 調整，或 `/settings reset` 重設。")
        return "\n".join(lines)
