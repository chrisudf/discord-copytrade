"""
moomoo OpenAPI 下单封装

职责：
- 构造 option_code、计算 limit_price（分档 slippage）、调用 SDK
- 不做风控（已迁移到 risk_manager）
- 同步函数，调用方需 asyncio.to_thread 包装

设计：
- ctx 单例懒加载（首次 place_order 才连 OpenD）
- account_id 从 .env 读，写死 MOOMOO_ACC_ID
- unlock_trade 只解一次（模拟盘其实不需要，留着兼容真实盘）

Slippage 分档（基于 6/17 HOOD 4-bagger 实战教训）：
- price <  $1.5  → 12%   # lotto / 低价单 spread 宽
- price <  $3.0  →  8%
- price >= $3.0  →  5%
TODO: 用 Polygon 回测后用真实 fill 数据校准这三档

返回格式约定（成功 / 失败统一字段）：
{
    "success": bool,
    "message": str,
    "order_id": str | None,
    "code": str,
    "qty": int,
    "price": float,
}
"""
import os
from datetime import date
from src.utils.logger import logger

# ---- 配置（从 .env 读取） ----
DEFAULT_QTY = int(os.getenv("DEFAULT_QTY", 1))
# .strip().upper() 防止 .env 写成 "simulate" / " REAL " 之类导致下面所有 == 判断失效
TRD_ENV_STR = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
OPEND_HOST = os.getenv("MOOMOO_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("MOOMOO_PORT", 11111))
# .env 里统一 MOOMOO_TRD_* 前缀（TRD_ENV / TRD_PWD），跟原 MOOMOO_TRADE_PWD 对齐
# 模拟盘 SIMULATE 不需要密码所以历史没发现这个 typo，上真盘前必修
# 兼容老 .env 里的 MOOMOO_TRADE_PWD：旧名字命中时打个 warn 提示迁移
_legacy_pwd = os.getenv("MOOMOO_TRADE_PWD")
TRADE_PWD = os.getenv("MOOMOO_TRD_PWD") or _legacy_pwd or ""
if _legacy_pwd and not os.getenv("MOOMOO_TRD_PWD"):
    logger.warning(
        "[broker] 检测到旧 env 名 MOOMOO_TRADE_PWD，建议改为 MOOMOO_TRD_PWD（见 .env.example）"
    )
ACC_ID = int(os.getenv("MOOMOO_ACC_ID", 0))

# ---- SDK 导入 ----
try:
    from moomoo import (
        OpenSecTradeContext, TrdMarket, SecurityFirm,
        TrdSide, OrderType, TrdEnv, RET_OK,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    logger.warning("[broker] moomoo SDK 未安装，仅 DRY_RUN 可用")

# ---- 模块级单例 ----
_ctx = None
_unlocked = False

# get_last_price 真盘路径日志去重状态（见 get_last_price 注释）
_real_quote_warned_once: bool = False
_real_quote_warned_codes: set[str] = set()


def _reset_ctx():
    """连接异常后重置单例，下次调用会重连。
    OpenD 重启或网络抖动会让旧 ctx 永久变坏，必须显式丢弃。"""
    global _ctx, _unlocked
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:
            pass
    _ctx = None
    _unlocked = False
    logger.warning("[broker] ctx reset, will reconnect on next call")


def _is_dry_run() -> bool:
    """实时读取 DRY_RUN，避免 import 时锁定导致测试无法覆盖真实分支"""
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _get_trd_env():
    """字符串配置 → SDK 枚举"""
    return TrdEnv.REAL if TRD_ENV_STR == "REAL" else TrdEnv.SIMULATE


def _get_slippage_pct(price: float) -> float:
    """
    分档 slippage：低价合约 spread 更宽，需要更大偏移才追得到 fill。

    分档（v1，待回测校准）：
    - < $1.5  → 12%
    - < $3.0  →  8%
    - >= $3.0 →  5%

    例子：
    - HOOD 1.10 → 1.10 * 1.12 = 1.23   (旧版 5% 只挂 1.16，6/17 漏吃 4-bagger)
    - NOW  3.90 → 3.90 * 1.05 = 4.09   (与旧版一致)
    - IREN 0.68 → 0.68 * 1.12 = 0.76
    """
    if price < 1.5:
        return 0.12
    elif price < 3.0:
        return 0.08
    else:
        return 0.05


def _calc_limit_price(price: float) -> float:
    """
    计算挂单限价 = entry_price * (1 + slippage_pct)，2 位小数。

    TODO: 加入 Penny Pilot tick 档位对齐
    - Penny Pilot (IREN/SPY/QQQ/HOOD 等)：tick = $0.01，当前 round(2) 已对齐
    - 非 Penny：< $3 → $0.05 / >= $3 → $0.10，当前会挂出非法价位
    暂不实现，等收集到非 Penny 拒单数据再做。
    """
    pct = _get_slippage_pct(price)
    return round(price * (1 + pct), 2)


def _get_ctx():
    """懒加载 ctx 单例。失败抛异常由调用方 catch。"""
    global _ctx
    if _ctx is None:
        if not SDK_AVAILABLE:
            raise RuntimeError("moomoo SDK not installed")
        logger.info(f"[broker] 连接 OpenD {OPEND_HOST}:{OPEND_PORT}")
        _ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=OPEND_HOST,
            port=OPEND_PORT,
            security_firm=SecurityFirm.FUTUINC,
        )
    return _ctx


def _ensure_account():
    """直接返回 .env 配置的账号 ID。"""
    if ACC_ID == 0:
        raise RuntimeError("MOOMOO_ACC_ID 未配置")
    return ACC_ID


def _ensure_unlocked():
    """首次调用解锁交易。

    moomoo SIMULATE 模式 **不支持** unlock_trade —— 调它必返回
    "ERROR. No one available account!"（即使 acc_status=ACTIVE）。
    因此 SIMULATE 永远跳过，无论 TRADE_PWD 是否配置。
    """
    global _unlocked
    if _unlocked:
        return
    if TRD_ENV_STR == "SIMULATE":
        logger.info("[broker] SIMULATE 模式，跳过 unlock_trade（moomoo 不支持）")
        _unlocked = True
        return
    ctx = _get_ctx()
    if not TRADE_PWD:
        raise RuntimeError("REAL 模式但 MOOMOO_TRD_PWD 未配置，无法 unlock_trade")
    ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
    if ret != RET_OK:
        raise RuntimeError(f"unlock_trade failed: {data}")
    _unlocked = True
    logger.info("[broker] unlock_trade OK (REAL)")


# moomoo 会话/账户失效返回的关键字（小写匹配）。命中时 reset ctx + 重试一次。
# 之所以放白名单：避免把"价格无效""超额"等业务拒单也当成 stale 反复重试。
_STALE_SESSION_HINTS = (
    "no one available account",   # SIMULATE 会话过期 / 账户挂起
    "account is not unlock",      # 真盘 unlock 状态丢失
    "session",                    # 通用 session 失效
    "not login",                  # OpenD 失连
)


def _is_stale_session(msg: str) -> bool:
    s = (msg or "").lower()
    return any(h in s for h in _STALE_SESSION_HINTS)


def breakeven_exit_price(entry_price: float, sell_slip: float = 0.05) -> tuple[float, float]:
    """计算"我们跟单不亏"所需的最低 KC 卖出价（毛 PnL %）。

    我们买入 ≈ entry × (1 + buy_slip)
    我们卖出 ≈ KC_exit × (1 - sell_slip)
    净 PnL = 0 → KC_exit = entry × (1 + buy_slip) / (1 - sell_slip)

    返回 (breakeven_price, breakeven_gross_pct)
    例: entry=2.70（$1.5-$3 档，buy_slip=8%）→ (3.07, +13.7%)

    实战价值：开单时 TG 提示 KC 至少要 +N% 退出我们才不亏，
    用户对照 KC 历史 trim 阈值（通常 +50%/+100%）能直观判断信号好坏。
    """
    if entry_price is None or entry_price <= 0:
        return 0.0, 0.0
    buy = _get_slippage_pct(entry_price)
    be_price = entry_price * (1 + buy) / (1 - sell_slip)
    be_pct = (be_price / entry_price - 1) * 100
    return round(be_price, 2), round(be_pct, 1)


def build_option_code(symbol: str, exp_date: date, strike: float, side: str) -> str:
    """
    构造 moomoo 期权代码

    格式：US.{SYMBOL}{YYMMDD}{C/P}{STRIKE*1000:06d}
    例子：IREN 60C exp 2026-06-15 → US.IREN260615C060000

    注意：
    - strike * 1000 是因为 moomoo 用千分之一美元为单位
    - 6 位前导零填充（不是 8 位！）
      strike=2.5  → 002500
      strike=60   → 060000
      strike=400  → 400000
    """
    date_str = exp_date.strftime("%y%m%d")
    cp = "C" if side == "CALL" else "P"
    strike_str = f"{int(strike * 1000):06d}"
    return f"US.{symbol}{date_str}{cp}{strike_str}"


def place_order(signal: dict, qty: int = None) -> dict:
    """
    下单（同步函数，调用方需用 asyncio.to_thread 包装）

    参数:
        signal: dict, 必含 symbol, expiry_date, strike, side, price
        qty: 不传则用 DEFAULT_QTY

    返回: 见模块顶部约定
    """
    qty = qty if qty is not None else DEFAULT_QTY

    option_code = build_option_code(
        signal["symbol"], signal["expiry_date"],
        signal["strike"], signal["side"],
    )
    entry_price = signal["price"]
    limit_price = _calc_limit_price(entry_price)
    slip_pct = _get_slippage_pct(entry_price)

    dry_run = _is_dry_run()
    logger.info(
        f"[broker] Order: {option_code} x {qty} @ {limit_price:.2f} "
        f"(entry={entry_price:.2f}, slip={slip_pct*100:.0f}%) "
        f"[env={TRD_ENV_STR}, dry_run={dry_run}] tags={signal.get('tags')}"
    )

    # ---- DRY_RUN 路径 ----
    if dry_run:
        return {
            "success": True, "message": "DRY_RUN",
            "order_id": "MOCK_001", "code": option_code,
            "qty": qty, "price": limit_price,
        }

    # ---- 真实下单 ----
    # TODO: 6/23 OSCR 260626 $30 call broker 拒单 "Cannot find ... in US Stocks"。
    # 可能原因：(a) 这个 strike+expiry 组合根本不存在；(b) option_code 编码格式问题。
    # 防御方案：调 quote_ctx.get_option_chain(SYMBOL, expiry, expiry) 验证 strike
    # 存在再下单。代价：每单多 1 次 API + 等待 quote_ctx 单例落地（见
    # docs/realtime_quote_design.md）。当前先靠 broker 拒单 + TG 告警，等
    # quote_ctx 上线后再加预校验。
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        order_args = dict(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,  # 限价单
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark="discord_auto",
        )
        ret, data = ctx.place_order(**order_args)
        # 中途 session 失效 → 重连 + 重试一次。避免昨晚 SNOW 那种"运行半夜忽然账户挂"丢信号
        if ret != RET_OK and _is_stale_session(str(data)):
            logger.warning(f"[broker] stale session ({data}), reset 后重试一次")
            _reset_ctx()
            ctx = _get_ctx()
            _ensure_unlocked()
            ret, data = ctx.place_order(**order_args)
        if ret == RET_OK:
            order_id = str(data["order_id"].iloc[0])
            logger.info(f"[broker] 下单成功 order_id={order_id}")
            return {
                "success": True, "message": "submitted",
                "order_id": order_id, "code": option_code,
                "qty": qty, "price": limit_price,
            }
        else:
            logger.error(f"[broker] 下单失败: {data}")
            return {
                "success": False, "message": str(data),
                "order_id": None, "code": option_code,
                "qty": qty, "price": limit_price,
            }
    except Exception as e:
        logger.exception("[broker] place_order 异常")
        _reset_ctx()
        return {
            "success": False, "message": str(e),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }


def place_sell_order(
    option_code: str,
    qty: int,
    limit_price: float,
    remark: str = "auto_close",
) -> dict:
    """卖单（限价，同步，调用方需 to_thread 包装）

    Args:
        option_code: 标的代码（同 build_option_code 输出）
        qty: 卖出张数
        limit_price: 限价。SL/EOD 场景建议传 bid * 0.95 偏激进确保成交；
                     CLOSE 信号正常 trim 可传 bid 附近。
        remark: 标记触发源（kc_close / sl_polling / tp_polling / eod_force）

    返回:
        {success, message, order_id, code, qty, price}

    TODO（测试调整）：
    - 实测后看是否要支持 OrderType.MARKET（SL 紧急情况）
    - 模拟盘 SIMULATE 卖单是否需要先有真实持仓，没有的话 SDK 会拒
    - 部分成交（dealt_qty < qty）的处理 —— 当前只看 RET_OK，不轮询 fill
    """
    if qty <= 0:
        return {
            "success": False, "message": f"invalid qty={qty}",
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }

    dry_run = _is_dry_run()
    logger.info(
        f"[broker] SELL: {option_code} x {qty} @ {limit_price:.2f} "
        f"[env={TRD_ENV_STR}, dry_run={dry_run}, remark={remark}]"
    )

    if dry_run:
        return {
            "success": True, "message": "DRY_RUN sell",
            "order_id": f"MOCK_SELL_{option_code[-6:]}",
            "code": option_code, "qty": qty, "price": limit_price,
        }

    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        _ensure_unlocked()

        order_args = dict(
            price=limit_price,
            qty=qty,
            code=option_code,
            trd_side=TrdSide.SELL,
            order_type=OrderType.NORMAL,  # 限价
            trd_env=_get_trd_env(),
            acc_id=acc_id,
            remark=remark,
        )
        ret, data = ctx.place_order(**order_args)
        if ret != RET_OK and _is_stale_session(str(data)):
            logger.warning(f"[broker] sell stale session ({data}), reset 后重试一次")
            _reset_ctx()
            ctx = _get_ctx()
            _ensure_unlocked()
            ret, data = ctx.place_order(**order_args)
        if ret == RET_OK:
            order_id = str(data["order_id"].iloc[0])
            logger.info(f"[broker] 卖单成功 order_id={order_id}")
            return {
                "success": True, "message": "submitted",
                "order_id": order_id, "code": option_code,
                "qty": qty, "price": limit_price,
            }
        logger.error(f"[broker] 卖单失败: {data}")
        return {
            "success": False, "message": str(data),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }
    except Exception as e:
        logger.exception("[broker] place_sell_order 异常")
        _reset_ctx()
        return {
            "success": False, "message": str(e),
            "order_id": None, "code": option_code,
            "qty": qty, "price": limit_price,
        }


def query_order_status(order_id: str) -> dict:
    """
    查单状态（同步）

    返回:
        {success, status, filled_qty, filled_avg_price, message}
    """
    if _is_dry_run():
        return {
            "success": True, "status": "FILLED_ALL",
            "filled_qty": 1, "filled_avg_price": 0.0,
            "message": "DRY_RUN",
        }
    try:
        ctx = _get_ctx()
        acc_id = _ensure_account()
        ret, data = ctx.order_list_query(
            order_id=order_id,
            trd_env=_get_trd_env(),
            acc_id=acc_id,
        )
        if ret != RET_OK:
            return {"success": False, "message": str(data),
                    "status": None, "filled_qty": 0, "filled_avg_price": 0.0}
        if len(data) == 0:
            return {"success": False, "message": "order not found",
                    "status": None, "filled_qty": 0, "filled_avg_price": 0.0}
        row = data.iloc[0]
        dealt_price = row.get("dealt_avg_price", 0)
        return {
            "success": True,
            "status": str(row["order_status"]),
            "filled_qty": int(row["dealt_qty"]),
            "filled_avg_price": float(dealt_price) if dealt_price else 0.0,
            "message": "ok",
        }
    except Exception as e:
        logger.exception("[broker] query_order_status 异常")
        _reset_ctx()
        return {"success": False, "message": str(e),
                "status": None, "filled_qty": 0, "filled_avg_price": 0.0}


def get_last_price(option_code: str):
    """查期权最新成交价。SL / EOD watcher 用。

    返回:
        float 最新价；None 表示拿不到（watcher 应跳过该仓位）

    DRY_RUN 路径：
        - 默认返回 None（不误触发 SL）
        - 设 MOCK_LAST_PRICE=0.5 强制固定价，方便联调 SL 阈值
        - 设 MOCK_LAST_PRICE_<CODE>=0.5 针对单 option_code 设价

    TODO（实测调整）：
    - 上真盘换 OpenQuoteContext.get_market_snapshot([code])，取 last_price 字段
    - quote_ctx 单独维护（和 trade_ctx 分开），同样懒加载 + reset 策略
    - 批量查询：watcher 一次拿一批 code 比 N 次单查省 N 倍 RTT
    - 行情订阅 vs 快照：snapshot 简单，但延迟高；订阅推送实时但要状态管理

    TODO（Discord 重连相关）：
    6/18 出现 29 次 Discord 重连（vs 之前 2-4 次/天）。
    怀疑 moomoo SDK 在 OPRA 拒绝路径下偶尔挂线程，间接占住 event loop。
    待 6/19 起 on_disconnect 日志收集数据后，若仍异常：
    - 给真盘路径加 asyncio.wait_for(timeout=2.0)
    - 给 broker 调用统一加 timeout，避免 SDK 卡死拖垮 watcher
    """
    if _is_dry_run():
        per_code = os.getenv(f"MOCK_LAST_PRICE_{option_code}")
        if per_code:
            return float(per_code)
        fixed = os.getenv("MOCK_LAST_PRICE")
        if fixed:
            return float(fixed)
        return None

    # TODO: 真盘走 quote_ctx.get_market_snapshot
    # 日志去重：每个 code 只 warn 一次（避免 watcher tick 每 5s 刷屏）
    # 同时首次进入真盘路径时打一次响亮警告，说明 SL/TP/EOD 在真盘不会触发
    global _real_quote_warned_once
    if not _real_quote_warned_once:
        _real_quote_warned_once = True
        logger.error(
            "[broker] ⚠️  真盘 get_last_price 未实现：SL / TP / EOD watcher 将无法触发！"
            " 0DTE 仓位会持有到 expire，请手动监控。详见 TODO。"
        )
    if option_code not in _real_quote_warned_codes:
        _real_quote_warned_codes.add(option_code)
        logger.warning(f"[broker] get_last_price not implemented for real env: {option_code}")
    return None


def probe_broker() -> tuple[bool, str]:
    """启动时探测 broker 健康度。供 run_listener 在 client.start 前调用。

    检查项：
      1. SDK 装好了吗
      2. OpenD 连得上吗
      3. get_acc_list 返回里有没有 MOOMOO_ACC_ID + MOOMOO_TRD_ENV 匹配的活跃账户
      4. REAL 模式：unlock_trade 测一下密码

    DRY_RUN=true 时跳过整套探测，直接返回 OK。

    Returns:
        (ok, message)：ok=False 时 message 是用户可读的诊断原因
    """
    # 启动时把生效配置打到日志，方便人眼对比 .env，避免"以为改了实际没生效"
    logger.info(
        f"[broker] config snapshot: TRD_ENV={TRD_ENV_STR} ACC_ID={ACC_ID} "
        f"TRADE_PWD={'set' if TRADE_PWD else 'empty'} "
        f"OpenD={OPEND_HOST}:{OPEND_PORT} DRY_RUN={_is_dry_run()}"
    )
    if _is_dry_run():
        return True, "DRY_RUN: 跳过 broker 探测"
    if not SDK_AVAILABLE:
        return False, "moomoo SDK 未安装（pip install moomoo-api）"
    if ACC_ID == 0:
        return False, "MOOMOO_ACC_ID 未配置或为 0"

    try:
        ctx = _get_ctx()
    except Exception as e:
        return False, f"连接 OpenD {OPEND_HOST}:{OPEND_PORT} 失败: {e}"

    try:
        ret, df = ctx.get_acc_list()
    except Exception as e:
        _reset_ctx()
        return False, f"get_acc_list 抛异常: {type(e).__name__}: {e}"

    if ret != RET_OK:
        return False, f"get_acc_list 失败: {df}"
    if df is None or len(df) == 0:
        return False, (
            "OpenD 没返回任何账户。检查：\n"
            "  1) moomoo 桌面端是否已登录\n"
            "  2) SIMULATE: 需要在桌面端启用模拟交易并选过一个账户\n"
            "  3) 试着重启 OpenD 或重登桌面端"
        )

    # 匹配账户
    try:
        matched = df[(df["acc_id"] == ACC_ID) & (df["trd_env"] == TRD_ENV_STR)]
    except Exception as e:
        return False, f"过滤账户列异常: {e}（df.columns={getattr(df, 'columns', '?').tolist() if hasattr(df, 'columns') else '?'}）"

    if len(matched) == 0:
        ids = df["acc_id"].tolist() if "acc_id" in df.columns else []
        envs = df["trd_env"].tolist() if "trd_env" in df.columns else []
        return False, (
            f"MOOMOO_ACC_ID={ACC_ID} env={TRD_ENV_STR} 不在可用账户列表里。\n"
            f"  OpenD 返回的 acc_id: {ids}\n"
            f"  OpenD 返回的 trd_env: {envs}\n"
            f"  → 把 .env 里 MOOMOO_ACC_ID 改成上面其中之一"
        )

    status = matched.iloc[0].get("acc_status", "UNKNOWN")
    if status != "ACTIVE":
        return False, f"账户 {ACC_ID} 状态为 {status}（非 ACTIVE），无法下单"

    # REAL 模式：试 unlock。SIMULATE 由 _ensure_unlocked 直接 skip，这里不碰
    if TRD_ENV_STR == "REAL":
        if not TRADE_PWD:
            return False, "REAL 模式但 MOOMOO_TRD_PWD 未配置"
        try:
            ret, data = ctx.unlock_trade(password=TRADE_PWD, is_unlock=True)
            if ret != RET_OK:
                return False, f"REAL unlock_trade 失败: {data}（密码错？）"
            global _unlocked
            _unlocked = True
        except Exception as e:
            return False, f"unlock_trade 抛异常: {type(e).__name__}: {e}"

    return True, (
        f"OK · env={TRD_ENV_STR} acc_id={ACC_ID} status={status} "
        f"({len(matched)} matching / {len(df)} total)"
    )


def close_ctx():
    """优雅退出时调用。"""
    global _ctx, _unlocked
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:
            pass
        _ctx = None
        _unlocked = False
        logger.info("[broker] ctx closed")