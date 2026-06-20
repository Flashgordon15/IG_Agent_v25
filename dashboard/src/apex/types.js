/**
 * @typedef {Object} ApexTickPayload
 * @property {string} type
 * @property {string} ts
 * @property {number | null} bid
 * @property {number | null} offer
 * @property {number | null} spread
 * @property {string} market_state
 * @property {string} stream_status
 * @property {number | null} balance_gbp
 * @property {number | null} daily_pnl_gbp
 * @property {number | null} concurrent_risk_gbp
 * @property {Record<string, unknown>} markets
 * @property {Record<string, unknown>} avionics_assets
 * @property {Record<string, unknown>} avionics_hud
 * @property {Record<string, unknown> | null} health
 * @property {Record<string, unknown> | null} config
 * @property {Record<string, unknown> | null} points
 * @property {unknown} trading_healthy
 * @property {unknown} quotes_fresh
 * @property {number | null} agent_pid
 * @property {Record<string, unknown>} raw
 */

/**
 * @typedef {Object} AvionicsAssetTelemetry
 * @property {string} assetKey
 * @property {string} epic
 * @property {number | null} mid
 * @property {number | null} bid
 * @property {number | null} offer
 * @property {number | null} spread
 * @property {number | null} confidence
 * @property {number | null} rsi
 * @property {string} direction
 * @property {string} marketState
 * @property {string} streamStatus
 * @property {string} blocker
 * @property {number | null} [fitness]
 * @property {number | null} [volatility]
 * @property {Record<string, unknown> | null} [health]
 */

/**
 * @typedef {Object} PillarRow
 * @property {string} id
 * @property {string} label
 * @property {string} key
 * @property {'active' | 'degraded' | 'blocked'} status
 * @property {string} detail
 */

/**
 * @typedef {Object} PillarTelemetry
 * @property {number} baselineEquityGbp
 * @property {number} portfolioEnvelopeGbp
 * @property {number} concurrentRiskGbp
 * @property {number} envelopeUtilPct
 * @property {number} mlVetoFloor
 * @property {number | null} mlProbability
 * @property {boolean} mlUnblocked
 * @property {PillarRow[]} pillars
 */

/**
 * @typedef {Object} OperationalTransparency
 * @property {Record<string, number>} funnel
 * @property {Object} health_grid
 * @property {Array<{ts_utc: string, line: string}>} micro_ticker
 * @property {Object} [ml_post_mortem]
 */

/**
 * @typedef {Object} ParsedApexTelemetry
 * @property {ApexTickPayload} tick
 * @property {number} receivedAt
 * @property {Record<string, AvionicsAssetTelemetry>} assets
 * @property {PillarTelemetry} pillars
 * @property {OperationalTransparency | null} transparency
 */

/**
 * @typedef {Object} ApexIpcStatus
 * @property {boolean} connected
 * @property {string} [transport]
 * @property {number} [lastTickAt]
 * @property {boolean} [degraded]
 */

export {};
