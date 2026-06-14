"""
Channel configuration loader.
读取 config/channels.json，提供按 channel_id 查询配置的接口。
"""
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChannelConfig:
    channel_id: int
    name: str
    trigger_user_ids: list[int]
    default_qty: int
    max_price: float
    enabled: bool

    def is_trigger_user(self, user_id: int) -> bool:
        return user_id in self.trigger_user_ids


class ChannelRegistry:
    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[2] / "config" / "channels.json"
        self.config_path = config_path
        self._channels: dict[int, ChannelConfig] = {}
        self.reload()

    def reload(self):
        if not self.config_path.exists():
            raise FileNotFoundError(f"channels.json 不存在: {self.config_path}")
        with open(self.config_path) as f:
            raw = json.load(f)
        self._channels = {}
        for cid_str, cfg in raw.items():
            cid = int(cid_str)
            self._channels[cid] = ChannelConfig(
                channel_id=cid,
                name=cfg["name"],
                trigger_user_ids=[int(u) for u in cfg["trigger_user_ids"]],
                default_qty=int(cfg["default_qty"]),
                max_price=float(cfg["max_price"]),
                enabled=bool(cfg.get("enabled", True)),
            )

    def get(self, channel_id: int) -> Optional[ChannelConfig]:
        return self._channels.get(channel_id)

    def is_monitored(self, channel_id: int) -> bool:
        cfg = self._channels.get(channel_id)
        return cfg is not None and cfg.enabled

    def all_channel_ids(self) -> list[int]:
        return list(self._channels.keys())

    def enabled_channel_ids(self) -> list[int]:
        return [cid for cid, cfg in self._channels.items() if cfg.enabled]


# 全局单例
registry = ChannelRegistry()
