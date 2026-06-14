"""
频道配置加载器
读取 config/channels.json，提供查询接口
"""
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from loguru import logger

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "channels.json"


@dataclass
class ChannelConfig:
    """单个频道的配置"""
    channel_id: str           # Discord channel ID (str 形式)
    name: str                 # 显示名
    trigger_user_ids: set     # 允许触发的 user ID 集合（int）
    default_qty: int          # 默认下单张数
    max_price: float          # 单张价格上限（频道级，叠加全局风控）
    enabled: bool             # 是否启用
    
    def is_trigger_user(self, user_id: int) -> bool:
        """检查 user_id 是否是这个频道的触发人"""
        return int(user_id) in self.trigger_user_ids


class ChannelRegistry:
    """全局频道注册表"""
    
    def __init__(self):
        self._channels: dict[str, ChannelConfig] = {}
        self.load()
    
    def load(self):
        """从 JSON 加载"""
        if not CONFIG_PATH.exists():
            logger.error(f"[Config] 频道配置文件不存在: {CONFIG_PATH}")
            return
        
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"[Config] JSON 解析失败: {e}")
            return
        
        self._channels.clear()
        for ch_id, cfg in data.items():
            try:
                self._channels[str(ch_id)] = ChannelConfig(
                    channel_id=str(ch_id),
                    name=cfg["name"],
                    trigger_user_ids=set(int(u) for u in cfg["trigger_user_ids"]),
                    default_qty=int(cfg.get("default_qty", 1)),
                    max_price=float(cfg.get("max_price", 5.0)),
                    enabled=bool(cfg.get("enabled", True)),
                )
            except (KeyError, ValueError, TypeError) as e:
                logger.error(f"[Config] 频道 {ch_id} 配置错误: {e}")
                continue
        
        enabled_count = sum(1 for c in self._channels.values() if c.enabled)
        logger.info(f"[Config] 加载 {len(self._channels)} 个频道，{enabled_count} 个启用")
        for ch in self._channels.values():
            status = "✅" if ch.enabled else "⏸️ "
            logger.info(
                f"  {status} {ch.channel_id} | {ch.name} | "
                f"qty={ch.default_qty} max=${ch.max_price} "
                f"triggers={len(ch.trigger_user_ids)}"
            )
    
    def get(self, channel_id) -> Optional[ChannelConfig]:
        """根据 channel_id 拿配置（None = 不监听这个频道）"""
        cfg = self._channels.get(str(channel_id))
        if cfg and not cfg.enabled:
            return None
        return cfg
    
    def all_enabled_ids(self) -> list[str]:
        """返回所有启用的频道 ID"""
        return [c.channel_id for c in self._channels.values() if c.enabled]
    
    def is_monitored(self, channel_id) -> bool:
        """快速判断频道是否被监听"""
        return self.get(channel_id) is not None
    
    def reload(self):
        """热加载（未来扩展，比如 SIGHUP）"""
        logger.info("[Config] 重新加载频道配置")
        self.load()


# 全局单例
registry = ChannelRegistry()