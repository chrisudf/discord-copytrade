"""
Telegram 通知模块
"""
import os
import asyncio
import httpx
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

# 绝对路径加载 .env
ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(ENV_PATH, override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# 超时设置
TIMEOUT = 5.0  # 秒，不能太长，避免阻塞主流程


async def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """
    发送 Telegram 消息
    
    Args:
        text: 消息内容
        parse_mode: "Markdown" 或 "HTML" 或 None
    
    Returns:
        True 成功，False 失败（失败只记日志，不抛异常）
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[Telegram] BOT_TOKEN 或 CHAT_ID 未配置，跳过通知")
        return False
    
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(API_URL, json=payload)
            if resp.status_code == 200:
                logger.debug(f"[Telegram] 发送成功: {text[:50]}...")
                return True
            # 400 = Markdown 解析失败，fallback 到纯文本重试一次
            if resp.status_code == 400 and parse_mode:
                logger.warning(f"[Telegram] Markdown 解析失败，fallback 纯文本: {resp.text[:120]}")
                payload.pop("parse_mode", None)
                resp2 = await client.post(API_URL, json=payload)
                if resp2.status_code == 200:
                    logger.debug("[Telegram] 纯文本 fallback 成功")
                    return True
                logger.error(f"[Telegram] fallback 也失败 status={resp2.status_code} body={resp2.text[:200]}")
                return False
            logger.error(f"[Telegram] 发送失败 status={resp.status_code} body={resp.text[:200]}")
            return False
    except httpx.TimeoutException:
        logger.error(f"[Telegram] 超时 ({TIMEOUT}s)")
        return False
    except Exception as e:
        logger.error(f"[Telegram] 异常: {e}")
        return False


# ============ 格式化辅助函数 ============

def format_signal_alert(channel_name: str, symbol: str, strike: float,
                        expiry: str, side: str, price: float, qty: int,
                        action: str = "OPEN",
                        breakeven: tuple = None) -> str:
    """格式化信号触发通知

    breakeven: (price, gross_pct_needed) —— 来自 broker.breakeven_exit_price
               显示"KC 至少 ≥ ${price} (+{gross}%) 退出我们才不亏"
    """
    emoji = "🟢" if side.upper() == "C" else "🔴"
    msg = (
        f"{emoji} *新信号触发*\n"
        f"频道: `{channel_name}`\n"
        f"标的: *{symbol}* {strike}{side.upper()} {expiry}\n"
        f"动作: {action}\n"
        f"价格: ${price}\n"
        f"数量: {qty} 张\n"
        f"成本: ${price * 100 * qty:.0f}"
    )
    if breakeven:
        be_price, be_pct = breakeven
        msg += f"\n📐 盈亏平衡: KC ≥ *${be_price}* (gross +{be_pct:.1f}%)"
    return msg


def format_order_filled(symbol: str, strike: float, side: str, expiry: str,
                        fill_price: float, qty: int, order_id: str) -> str:
    """格式化下单成功通知"""
    return (
        f"✅ *下单成功*\n"
        f"标的: *{symbol}* {strike}{side.upper()} {expiry}\n"
        f"成交价: ${fill_price}\n"
        f"数量: {qty} 张\n"
        f"订单号: `{order_id}`"
    )


def format_risk_blocked(reason: str, detail: str = "") -> str:
    """格式化风控拦截通知"""
    msg = f"⚠️ *风控拦截*\n原因: {reason}"
    if detail:
        msg += f"\n详情: {detail}"
    return msg


def format_error(scope: str, error: str) -> str:
    """格式化错误通知"""
    return f"❌ *系统错误*\n模块: `{scope}`\n错误: ```{error[:500]}```"


def format_close_filled(symbol: str, strike: float, side: str, expiry: str,
                        qty_sold: int, fill_price: float, pct: int,
                        trigger: str, order_id: str) -> str:
    """卖单成交通知"""
    return (
        f"💰 *平仓成交*\n"
        f"标的: *{symbol}* {strike}{side.upper()} {expiry}\n"
        f"卖出: {qty_sold} 张 @ ${fill_price} ({pct}%)\n"
        f"触发: `{trigger}`\n"
        f"订单号: `{order_id}`"
    )


def format_close_skipped(reason: str, raw: str) -> str:
    """CLOSE 信号收到但未执行（没匹配到持仓 / 解析跳过 / parser 拒绝）"""
    return (
        f"📭 *CLOSE 未执行*\n"
        f"原因: {reason}\n"
        f"原文: ```{raw[:300]}```"
    )


def format_daily_summary(orders: int, total_cost: float,
                         max_orders: int, max_cost: float) -> str:
    """格式化每日统计"""
    return (
        f"📊 *今日统计*\n"
        f"下单次数: {orders}/{max_orders}\n"
        f"累计成本: ${total_cost:.0f}/${max_cost:.0f}\n"
        f"剩余额度: ${max_cost - total_cost:.0f}"
    )


# ============ 同步包装（兼容非 async 调用方）============

def send_telegram_sync(text: str, parse_mode: str = "Markdown") -> bool:
    """同步版本，给非异步代码调用"""
    try:
        return asyncio.run(send_telegram(text, parse_mode))
    except RuntimeError:
        # 已在事件循环里
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(send_telegram(text, parse_mode))