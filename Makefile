# 常用命令快捷入口
# 用法: make <target>

PY := .venv/bin/python

.PHONY: help report listener test backfill analyze

help:
	@echo "可用命令:"
	@echo "  make report    刷新 CSV 回填 + 生成回测报告 (data/backtest_report.md)"
	@echo "  make backfill  仅刷新 backfill_open/close.csv (不分析)"
	@echo "  make analyze   仅基于现有 CSV 重跑分析"
	@echo "  make listener  启动 Discord listener"
	@echo "  make test      pytest 全跑"

# 完整流程：从 raw_signals → 解析 → 配对 → 报告
# 跑这个之前不需要停 listener（read-only）
report: backfill analyze
	@echo ""
	@echo "✅ 报告就绪："
	@echo "   - data/backfill_open.csv"
	@echo "   - data/backfill_close.csv"
	@echo "   - data/trades_KC_期权_波段.csv"
	@echo "   - data/trades_enrich.csv"
	@echo "   - data/backtest_report.md"

backfill:
	$(PY) scripts/backfill_history.py

analyze:
	$(PY) scripts/analyze_trades.py

listener:
	$(PY) scripts/run_listener.py

test:
	$(PY) -m pytest tests/ -q --ignore=tests/test_option_chain.py

# TODO（数据攒够后做真正的自动化，约 N≥30 / 3-4 周后）：
# 周报版本应该是：
#   - 周日 22:00 ET cron 触发
#   - 跑 make report
#   - 把 backtest_report.md 摘要发到 TG（胜率 / 净 PnL / 最佳最差 trade）
#   - 净胜率掉到 < 0% 立即额外 TG 警报
#   - 持续监控数据漂移：parser 失败率涨、no_exit_price 比例涨、never_closed 比例涨
# 实现思路：scripts/weekly_report.py + cron / launchd plist
