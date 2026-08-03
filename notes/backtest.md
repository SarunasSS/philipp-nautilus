```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO
  --start 2026-01-01 `
  --end 2026-06-01 `
  --catalog-path data/ES/catalog
  --starting-balance 100000 USD
  htf-sweep-cisd run `
  --htf-bar-type ES.c.0 GLBX-15-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --ltf-bar-type ES.c.0.GLBX-1-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --original-bar-type ES.c.0.GLBX-1-SECOND-LAST-EXTERNAL `
  --entry-order-type LIMIT `
  --entry-order-expire-minutes None `
  --entry-limit-offset 0.0 `
  --trade-notional 1_000.0 `
  --stop-order-type MARKET `
  --stop-loss-distance-ratio 1.0 `
  --take-profit-multiplier 2.0
  --stop-limit-offset 0.0 `
  --signal-cooldown-seconds None `
```