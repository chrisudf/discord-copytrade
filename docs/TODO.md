# TODO

## P0 - immediate
- [ ] Connect real moomoo API (uncomment broker block, test on account)
- [ ] Validate option code format on OpenD
- [ ] Securely store Discord token (consider keyring)

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
