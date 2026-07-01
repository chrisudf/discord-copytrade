# TODO

## P0 - immediate
- [ ] Connect real moomoo API (uncomment broker block, test on account)
- [ ] Validate option code format on OpenD
- [ ] Securely store Discord token (consider keyring)
- [ ] **Runner mode B (quote-driven trim decision)**
  - 前置依赖：OPRA subscription (US MarketOptions Lv1+)
  - 背景：当前策略 A（qty==1 且 pct<100 → skip trim）粗暴保 runner。
    真实场景更精细：早期跟 KC trim（+9%~40% 该出），中后期变 runner
    （+50% 以上留一张）。见 lessons.md #13。
  - 实现：在 close 流程里查 get_last_price(pos.code)，算 (last / avg_entry - 1) * 100 = 当前 PnL%。
    * PnL% < 50 → 正常跟 KC 的 pct 卖（对齐早期风险管理）
    * PnL% >= 50 且 qty==1 → 跳过 <100% trim（保 runner）
    * 100% close 信号总是执行
  - 需求：get_last_prices 真返价（等 OPRA），以及一个可调阈值（.env 里 RUNNER_MIN_PNL_PCT，默认 50）
  - 替换掉 calc_qty_to_sell 里的策略 A 分支

## P1 - position sizing
- [ ] Calculate qty based on account balance %
- [ ] Limit single order amount for 0DTE (e.g. <\$500)
- [ ] Daily max loss circuit breaker
- [ ] Deduplicate repeated signals on same symbol

## P2 - exit strategy
- [ ] Listen for "closed/trim/stopped" -> auto close
- [ ] Trailing stop
- [ ] Time stop (force close 0DTE before market close)

## P3 - reporting
- [ ] Pull actual fill price vs signal price -> slippage stats
- [ ] Win rate / PnL ratio / max drawdown
- [ ] Web dashboard (Streamlit)

## P4 - infra
- [ ] Dockerize
- [ ] Unit test coverage > 80%
- [ ] Multi-channel / multi-trigger support

## Listener-related (added 2025-06-XX)
- [ ] P2: Implement CLOSE signal handler
  - Parse target position from message
  - Look up open positions in SQLite
  - Submit close order via moomoo
- [ ] P3: Symbol blacklist (skip 0DTE on SPY/QQQ etc.)
- [ ] P3: Multi-signal strategy options
  - first (current)
  - high_delta (closest to ITM)
  - all (open all contracts)

## Real-env quotes (P3, added 2026-06-30)
- [ ] **Subscribe US MarketOptions Lv1** in moomoo app (我的 → 行情订阅)
  - Without this: probe_quote_access returns QUOTE_NO_PERMISSION,
    SL/TP/EOD watchers no-op in real env, validate_option_codes
    falls back to "let broker decide"
- [ ] After subscribe: implement P3 PR 2 (watcher batch prefetch)
  - Refactor sl_watcher / tp_watcher / eod_watcher: replace
    N×get_last_price per tick with one get_last_prices(codes)
  - Add asyncio.wait_for(timeout=2.0) around to_thread snapshot
    call (the 6/18 thread-pool starvation concern)
  - Wire SL/TP/EOD watchers to consume the batched dict
  - Test: mock OpenQuoteContext to confirm exactly 1 snapshot RTT
    per tick regardless of position count
  - Verify live: open 1-2 SIMULATE positions and watch SL fire on
    a fake MOCK_LAST_PRICE
- [ ] After subscribe: verify validate_option_codes catches real
  invalid contracts (TEM/DRAM 类) instead of falling back
