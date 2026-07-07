"""
Telegram 通知模块

设计要点：
- 全部走 async + 模块级 AsyncClient（复用 TCP/TLS 连接）
- MarkdownV2 + escape_md，避免 Discord 原文里的 _ * ` 之类导致 400
- 429 限速时按 retry_after sleep 后重试一次
- 失败只 log 不抛，绝不阻塞主流程
"""
import os
import re
import asyncio
import httpx
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from loguru import logger

ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(ENV_PATH, override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIMEOUT = 5.0
MAX_RETRY_ON_429 = 1

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
# 串行化发送，规避 Telegram 单 chat ~1 msg/s 限速
_send_lock = asyncio.Lock()


def _api_url() -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def aclose():
    """关闭模块级 client。在程序退出时调用。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# MarkdownV2 完整保留字符列表（含 `*` `_` `` ` ``——这些虽然我们也用作格式，
# 但在"外部输入"片段里必须转义，否则用户数据里恰好有 `_` 会被 Telegram 当下划线解析）。
# 关键约束：escape_md() 只能对外部输入（频道名 / symbol / 错误文本 / raw 等）调用，
# 绝不能对我们模板里主动写的 `*粗体*` / `` `code` `` 这种结构字符调用——否则格式会被吃掉。
# 完整列表见 https://core.telegram.org/bots/api#markdownv2-style
_MDV2_ESCAPE = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(s) -> str:
    """转义 MarkdownV2 里所有 reserved 字符。

    给"用户输入"用（频道名、symbol、错误文本、raw 等）。
    不给我们自己拼的 `*粗体*` 这种结构字符用。
    """
    if s is None:
        return ""
    text = str(s)
    return re.sub(rf"([{re.escape(_MDV2_ESCAPE)}])", r"\\\1", text)


async def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """发送 Telegram 消息。

    Args:
        text: 已经按 parse_mode 转义好的文本
        parse_mode: "MarkdownV2" / "HTML" / None（纯文本）

    Returns:
        True 成功；False 失败（失败只记日志，不抛异常）
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

    client = await _get_client()
    url = _api_url()

    async with _send_lock:
        for attempt in range(MAX_RETRY_ON_429 + 1):
            try:
                resp = await client.post(url, json=payload)
            except httpx.TimeoutException:
                logger.error(f"[Telegram] 超时 ({TIMEOUT}s)")
                return False
            except httpx.HTTPError as e:
                # 注意：不要直接把 e 塞日志——其 repr 会包含 request.url（含 token）
                logger.error(f"[Telegram] 网络错误: {type(e).__name__}")
                return False
            except Exception as e:
                logger.error(f"[Telegram] 异常: {type(e).__name__}: {e}")
                return False

            if resp.status_code == 200:
                logger.debug(f"[Telegram] 发送成功: {text[:50]}")
                return True

            if resp.status_code == 429 and attempt < MAX_RETRY_ON_429:
                retry_after = 1
                try:
                    retry_after = int(resp.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    pass
                logger.warning(f"[Telegram] 429 限流，{retry_after}s 后重试")
                await asyncio.sleep(retry_after)
                continue

            # 400 = 解析失败，fallback 到纯文本重发
            if resp.status_code == 400 and parse_mode:
                logger.warning(f"[Telegram] {parse_mode} 解析失败，fallback 纯文本: {resp.text[:160]}")
                payload.pop("parse_mode", None)
                try:
                    resp2 = await client.post(url, json=payload)
                except Exception as e:
                    logger.error(f"[Telegram] fallback 请求异常: {type(e).__name__}: {e}")
                    return False
                if resp2.status_code == 200:
                    logger.debug("[Telegram] 纯文本 fallback 成功")
                    return True
                logger.error(f"[Telegram] fallback 也失败 status={resp2.status_code} body={resp2.text[:200]}")
                return False

            logger.error(f"[Telegram] 发送失败 status={resp.status_code} body={resp.text[:200]}")
            return False

    return False


# ============ 格式化辅助函数 ============
# 注意：所有外部输入（来自 Discord / broker / 异常文本）都必须走 escape_md，
# 否则碰到 _ * ` 之类字符会 400，虽然有 fallback 但格式会丢。

def format_signal_alert(channel_name: str, symbol: str, strike: float,
                        expiry: str, side: str, price: float, qty: int,
                        action: str = "OPEN",
                        breakeven: Optional[tuple] = None,
                        tags: Optional[list] = None) -> str:
    """格式化信号触发通知。

    breakeven: (price, gross_pct_needed) —— 来自 broker.breakeven_exit_price
    tags: parser 抽出的标签列表 ['lotto', 'swing', 'scalp', 'day_trade']
    """
    emoji = "🟢" if side.upper() == "C" else "🔴"
    cost = price * 100 * qty
    msg = (
        f"{emoji} *新信号触发*\n"
        f"频道: `{escape_md(channel_name)}`\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}{escape_md(side.upper())} {escape_md(expiry)}\n"
        f"动作: {escape_md(action)}\n"
        f"价格: ${escape_md(f'{price}')}\n"
        f"数量: {qty} 张\n"
        f"成本: ${escape_md(f'{cost:.0f}')}"
    )
    if tags:
        tag_str = " ".join(f"`{escape_md(t)}`" for t in tags)
        msg += f"\n🏷️ {tag_str}"
    if breakeven:
        be_price, be_pct = breakeven
        msg += (
            f"\n📐 盈亏平衡: KC ≥ *${escape_md(f'{be_price}')}* "
            f"\\(gross \\+{escape_md(f'{be_pct:.1f}')}%\\)"
        )
    return msg


def format_order_filled(symbol: str, strike: float, side: str, expiry: str,
                        fill_price: float, qty: int, order_id: str) -> str:
    """格式化下单成功通知"""
    return (
        f"✅ *下单成功*\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}{escape_md(side.upper())} {escape_md(expiry)}\n"
        f"成交价: ${escape_md(f'{fill_price}')}\n"
        f"数量: {qty} 张\n"
        f"订单号: `{escape_md(order_id)}`"
    )


def format_risk_blocked(reason: str, detail: str = "") -> str:
    """格式化风控拦截通知"""
    msg = f"⚠️ *风控拦截*\n原因: {escape_md(reason)}"
    if detail:
        msg += f"\n详情: {escape_md(detail)}"
    return msg


def format_error(scope: str, error: str) -> str:
    """格式化错误通知

    长 traceback 截到 500 字符，完整 stack 通过 logger.exception 落地。
    """
    truncated = (error or "")[:500]
    return (
        f"❌ *系统错误*\n"
        f"模块: `{escape_md(scope)}`\n"
        f"错误: ```\n{escape_md(truncated)}\n```"
    )


def format_close_filled(symbol: str, strike: float, side: str, expiry: str,
                        qty_sold: int, fill_price: float, pct: int,
                        trigger: str, order_id: str) -> str:
    """卖单成交通知"""
    return (
        f"💰 *平仓成交*\n"
        f"标的: *{escape_md(symbol)}* {escape_md(strike)}{escape_md(side.upper())} {escape_md(expiry)}\n"
        f"卖出: {qty_sold} 张 @ ${escape_md(f'{fill_price}')} \\({pct}%\\)\n"
        f"触发: `{escape_md(trigger)}`\n"
        f"订单号: `{escape_md(order_id)}`"
    )


def format_close_skipped(reason: str, raw: str) -> str:
    """CLOSE 信号收到但未执行（没匹配到持仓 / 解析跳过 / parser 拒绝）"""
    return (
        f"📭 *CLOSE 未执行*\n"
        f"原因: {escape_md(reason)}\n"
        f"原文: ```\n{escape_md((raw or '')[:300])}\n```"
    )


def format_addon_alert(symbol: str, raw: str) -> str:
    """疑似加仓信号未执行（parser 不支持无 strike 的 add-on 简写）→ 提醒人工。

    背景 7/6：KC "small add SPY @ 1.86" ×4 全部静默 parse-fail，
    加仓被漏掉且无任何提醒（裸 ticker 不满足 _looks_like_open_attempt）。
    """
    return (
        f"➕ *疑似加仓信号未执行*\n"
        f"标的: *{escape_md(symbol)}* \\(已持仓\\)\n"
        f"parser 不支持无 strike 的加仓简写，如需跟加请手动下单\n"
        f"原文: ```\n{escape_md((raw or '')[:300])}\n```"
    )


def format_daily_summary(orders: int, total_cost: float,
                         max_orders: int, max_cost: float) -> str:
    """格式化每日统计"""
    remaining = max_cost - total_cost
    return (
        f"📊 *今日统计*\n"
        f"下单次数: {orders}/{max_orders}\n"
        f"累计成本: ${escape_md(f'{total_cost:.0f}')}/${escape_md(f'{max_cost:.0f}')}\n"
        f"剩余额度: ${escape_md(f'{remaining:.0f}')}"
    )


# ============ 同步包装 ============
# 仅供「真同步」入口使用（比如 main 启动前的健康检查脚本）。
# async 调用方请直接 `await send_telegram(...)`，不要绕道这里。

def send_telegram_sync(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """同步调用。**用纯同步 httpx，不复用模块级 async client/lock。**

    历史 bug：旧实现是 `asyncio.run(send_telegram(...))`，每次新建 event loop。
    `send_telegram` 里有模块级 `asyncio.Lock` 和 `httpx.AsyncClient`，第一次
    调用把它们绑到 loopA，loopA 退出后再次调用，loop B 操作"loopA 的锁/客户端"
    会抛 "RuntimeError: ... bound to a different event loop" 或留下未关闭的
    socket。改用纯同步 httpx.Client 后整个调用与 async 状态完全隔离。

    限制：仍然不能在 event loop 内调用（async 上下文请直接 await）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "send_telegram_sync 不能在 event loop 内调用，请直接 await send_telegram(...)"
        )

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

    url = _api_url()
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
    except httpx.TimeoutException:
        logger.error(f"[Telegram-sync] 超时 ({TIMEOUT}s)")
        return False
    except httpx.HTTPError as e:
        logger.error(f"[Telegram-sync] 网络错误: {type(e).__name__}")
        return False
    except Exception as e:
        logger.error(f"[Telegram-sync] 异常: {type(e).__name__}: {e}")
        return False

    if resp.status_code == 200:
        logger.debug(f"[Telegram-sync] 发送成功: {text[:50]}")
        return True

    # 400 解析失败 → 纯文本兜底重发一次
    if resp.status_code == 400 and parse_mode:
        logger.warning(f"[Telegram-sync] {parse_mode} 解析失败，fallback 纯文本: {resp.text[:160]}")
        payload.pop("parse_mode", None)
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp2 = client.post(url, json=payload)
            if resp2.status_code == 200:
                return True
            logger.error(f"[Telegram-sync] fallback 也失败 status={resp2.status_code} body={resp2.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"[Telegram-sync] fallback 请求异常: {type(e).__name__}: {e}")
            return False

    logger.error(f"[Telegram-sync] 发送失败 status={resp.status_code} body={resp.text[:200]}")
    return False
