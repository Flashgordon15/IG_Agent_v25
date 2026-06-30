# IG = Execution Only; Yahoo = Data Only

## Policy

| Responsibility | Provider | Examples |
|----------------|----------|----------|
| **Execution** | IG REST | `POST /positions/otc`, `PUT` stop/limit, `GET /confirms`, position sync |
| **Market data** | Yahoo Finance (+ hub cache) | Mid prices, Z-score windows, rotation velocity, volatility |
| **Signals** | Internal (Yahoo-fed) | Micro-channel, pierce Z, regime scoring |

## IG must NOT be used for

- Indicator computation (ATR, RSI, moving averages)
- Primary quote feed when Yahoo is available
- Volatility / Z-score bootstrap (use Yahoo poller + `dual_core_execution` mid ingest)

## Yahoo must NOT be used for

- Order placement or modification
- Broker confirm / deal status

## Enforcement

- `system/data_execution_policy.py` — labels REST calls; warns on non-essential IG market fetches during active Yahoo poll
- `dual_core_execution._fetch_multi_source_quote` — Yahoo preferred when hub age > 15s
- `feeder/yahoo_quote_poller.py` — primary external data plane
- `execution/ig_execution_guard.py` — pauses IG **orders** under rate limit; signals continue

## Architecture

```
Yahoo → hub / dual_core Z-scores → signals → trade_manager
                                              ↓ (orders only)
                                            IG REST
```
