经验教训与 Bug 修复记录
持续更新。每次踩坑后追加，按日期倒序。
目的：避免重复踩坑 + 快速回顾架构决策。

2026-06-17（周二 AEST 上午 / ET 周一晚）
背景
6/16 ET 实跑发现 QCOM/IREN 两单都 Cannot find ..260619..，0 真单。
表面是 moomoo 合约不存在，根因是 Juneteenth 6/19 休市 → 应前移到 6/18。
顺带挖出 3 个隐藏 Bug。

修复清单
1. Bug A：下单失败仍写 risk DB
症状：moomoo 返回错误 → record_order() 照样落 daily_orders → 占用每日 10 单额度 + 污染统计。

根因：discord_client.handle_message 没检查 order_result.get("success")。

修复：失败提前 return，TG 发 ❌ Order rejected by broker，DB 不写。

代码：src/listener/discord_client.py

python
if not order_result.get("success"):
    await notify_error(...)
    return  # 提前退出，不 record_order
2. Bug B：parser 主动 skip 触发 Parse failed 警告
症状：消息含 holding / into tomorrow / 价格区间 → parser 返回 None → listener 当解析失败发 TG 警告。

根因：None 同时表示「解析失败」和「主动跳过」，语义混淆。

修复：parser 区分两种返回：

None = 真·解析失败（应警告）
{"skip": "..."} = 主动跳过（静默）
代码：src/parser/signal_parser.py

python
return {"skip": "holding_or_remaining"}  # 主动 skip
return None  # 真失败
listener 端：

python
if signal is None:
    await notify_parse_failed(...)
elif signal.get("skip"):
    return  # 静默
3. Bug C：expiry 用本地日期算，回测会错
症状：parser 用 date.today() 算 expiry，但消息可能是历史的（回测）或跨时区的（AEST vs ET）。

根因：消息时间戳没传进 parser。

修复：

parse_signal(text, msg_ts: date = None) 加 msg_ts 参数
listener 用 message.created_at.astimezone(ET_TZ).date() 提取 ET 日期传入
代码：src/listener/discord_client.py

python
def _extract_et_date(message) -> date:
    return message.created_at.astimezone(ET_TZ).date()
4. Juneteenth 假日调整（主菜）
症状：6/16 信号 weekly → 算成 6/19 周五 → moomoo Cannot find ..260619..（6/19 是 Juneteenth 休市，CBOE 不挂这一天的合约）。

根因：parser 不知道美股假日，无脑算下周五。

修复：

新建 src/parser/holidays.py：硬编码 2026/2027 期权假日 set + is_trading_day() + adjust_to_trading_day()
parser 所有 expiry 出口（7 处 + finalize）包 _adjust_expiry()，落到非交易日自动前移
weekly 周四后 → 视作 0DTE（不算下周五）
代码：src/parser/holidays.py

python
US_OPTION_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
}
维护提醒：每年 12 月手动加下一年假日（2028 还没写）。

5. expiry 字符串与 expiry_date 不同步
症状：holiday 调整后 expiry_date=6/18 但 expiry="6/19" 字段没改 → TG/DB 显示 6/19 实际下单 6/18，对账困惑。

根因：7 个 return 出口都手写 expiry 字符串，调整 expiry_date 后忘了同步。

修复：抽 _finalize_signal() helper，return 前统一用 expiry_date.month/day 重写 expiry。

副作用："7DTE" / "weekly" 字段全变 "M/D" —— 是好事，对账更直观。

代码：src/parser/signal_parser.py

python
def _finalize_signal(sig: dict) -> dict:
    if sig.get("expiry_date"):
        d = sig["expiry_date"]
        sig["expiry"] = f"{d.month}/{d.day}"
    return sig
验证
✅ test_parser_rules.py 22/22 通过
✅ 6/19 端到端：SPY 700p 06/19 → log 显示 expiry 2026-06-19 → 2026-06-18 → option_code US.SPY260618P700000 → 下单成功 (order_id=2094607)
✅ TG 显示 expiry: 6/18（与实际下单一致）
✅ latency 1291ms（正常范围）
✅ DB 6/16 污染清理完成（QCOM/IREN failed + AAOI/CRWV DRY_RUN 残留）
教训
None 不能同时表示「失败」和「跳过」。多状态返回用 sentinel dict / Enum。
任何时间相关字段都要带时区。消息时间戳必传 message.created_at，不能 date.today()。
假日表必须硬编码，第三方库（pandas-market-calendars）依赖太重、版本风险大。
错误处理要早 return，不要让失败的状态继续往下流。
display 字段（字符串）必须从 source-of-truth（date 对象）派生，不要两边手写。
每次改 parser/risk 必须重启 listener，没有热加载。
已知待办
 smart_expiry 短距离（<2 天）过期日不跨年 → 6/15 信号在 6/17 不会算成 2027-06-15
 60s 未成交自动撤单
 day trade 关键词带空格的 tag 提取
 _strip_chinese 边界情况 fallback
 2028 假日表
 止盈止损策略设计（Q1-Q5）
模板（每次踩坑后追加）
每次新增一节用以下结构：

背景：一句话说明发生了什么
修复清单：每个 Bug 含 症状 / 根因 / 修复 / 代码 四段
验证：测试通过的项
教训：抽象出的通用规则
已知待办：未做完的尾巴
重要架构决策（不变量）
这些是踩过坑总结的、不要再改的决策。

时区：写入用 UTC+Z naive 防御，展示用 ET，trading_date 永远用 ET 日期。
失败提前 return：broker 失败 → 不 record_order，不进风控统计，不污染 daily limit。
parser 返回三态：
dict 成功
{"skip": "..."} 主动跳过（静默不通知）
None 真失败（TG 警告）
expiry 字符串从 expiry_date 派生，禁止手写。统一在 _finalize_signal() 处理。
假日 set 硬编码，每年 12 月加下一年。
指纹去重 5min 窗口，不含 price/channel。
修改 parser/risk 后必须重启 listener（无热加载）。
moomoo 模拟盘下单条件：DRY_RUN=false + TRD_ENV=SIMULATE 才会真下到模拟盘。
DRY_RUN 不导出到 broker 模块接口，测试脚本自读 env。
pip 包名 vs import 名：pip install moomoo-api，但 import moomoo。
同步 SDK 异步调用：moomoo SDK 同步，用 asyncio.to_thread() 包装。
.env 加载用绝对路径 + override=True，避免 IDE / shell 环境变量干扰。
self-bot 单账号挂载：Client.user 只读，不要尝试双连。
TG Markdown fallback：含 $ _ 等字符易解析失败，自动 fallback 纯文本。
0DTE 限额小仓：DEFAULT_QTY=1，MAX_COST_PER_ORDER=$500，MAX_DAILY_COST=$2000。
CBOE daily expiry：仅 SPY/QQQ/IWM + 头部股 M-F 每日；中小盘（如 IREN）仅 Friday weekly。
关键文件索引
bash
src/parser/signal_parser.py       # 4 规则解析 + _finalize_signal
src/parser/holidays.py            # 2026/2027 假日 set
src/listener/discord_client.py    # 信号路由 + Bug A/B/C
src/broker/moomoo_client.py       # 下单（不导出 DRY_RUN）
src/risk/risk_manager.py          # 风控 + UTC+Z 时间戳
src/notifier/telegram_client.py   # TG 推送（Markdown fallback）
src/storage/logger_db.py          # SQLite 写入
src/config/channel_loader.py     # channels.json 加载
config/.env                       # 环境变量
config/channels.json              # 频道配置
data/risk.db                      # daily_orders + circuit_breaker
data/trades.db                    # raw_signals + orders
logs/app_YYYY-MM-DD.log           # 应用日志
关键命令速查
启动 listener：

bash
source .venv/bin/activate && python -m scripts.run_listener
后台启动：

bash
nohup python -m scripts.run_listener > logs/listener.out 2>&1 &
echo $! > logs/listener.pid
停止：

bash
pkill -f run_listener
# 或
kill $(cat logs/listener.pid)
防 Mac 睡眠：

bash
caffeinate -i -d
查今日下单数：

bash
sqlite3 data/risk.db "SELECT COUNT(*), SUM(cost) FROM daily_orders WHERE trading_date='YYYY-MM-DD';"
查最近 raw signals：

bash
sqlite3 data/trades.db "SELECT msg_id, author, substr(content,1,80), received_at FROM raw_signals ORDER BY received_at DESC LIMIT 10;"
查最近订单：

bash
sqlite3 data/trades.db "SELECT id, symbol, side, strike, expiry, success, message FROM orders ORDER BY id DESC LIMIT 10;"
重置 daily limit（紧急用）：

bash
python scripts/reset_daily_limit.py
测试 parser：

bash
python scripts/test_parser_rules.py
端到端测试：

bash
python scripts/test_handle_message_real.py