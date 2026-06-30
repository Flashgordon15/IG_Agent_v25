use reqwest::Client;
use serde_json::{json, Value};
use std::time::Duration;

#[derive(Clone)]
pub struct BackendClient {
    base_url: String,
    http: Client,
}

impl BackendClient {
    pub fn new(base_url: impl Into<String>) -> Self {
        let http = Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .unwrap_or_else(|_| Client::new());
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            http,
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    async fn get_json(&self, path: &str) -> Result<Value, String> {
        let resp = self
            .http
            .get(self.url(path))
            .send()
            .await
            .map_err(|e| format!("GET {path}: {e}"))?;
        if !resp.status().is_success() {
            return Err(format!("GET {path}: HTTP {}", resp.status()));
        }
        resp.json::<Value>()
            .await
            .map_err(|e| format!("GET {path} decode: {e}"))
    }

    pub async fn fetch_agent_state(&self) -> Result<Value, String> {
        self.get_json("/api/state").await
    }

    pub async fn fetch_health_light(&self) -> Result<Value, String> {
        self.get_json("/api/health_light").await
    }

    pub async fn fetch_gui_status(&self) -> Result<Value, String> {
        self.get_json("/api/gui_status").await
    }

    pub async fn fetch_pnl(&self) -> Result<Value, String> {
        if let Ok(body) = self.get_json("/api/pnl").await {
            return Ok(body);
        }
        let state = self.get_json("/state").await?;
        let gui = self.fetch_gui_status().await?;
        Ok(json!({
            "source": "derived",
            "daily_pnl_gbp": state.get("daily_pnl_gbp"),
            "balance_gbp": state.get("balance_gbp"),
            "points": state.get("points"),
            "daily_pnl_targeting": gui.get("daily_pnl_targeting"),
            "session_review": gui.get("session_review"),
            "ts": gui.get("ts").or_else(|| state.get("ts")),
        }))
    }

    pub async fn fetch_routing(&self) -> Result<Value, String> {
        if let Ok(agent) = self.fetch_agent_state().await {
            if agent.get("routing").and_then(|v| v.as_array()).map(|a| !a.is_empty()).unwrap_or(false) {
                return Ok(json!({
                    "source": "agent_state",
                    "unified_execution_route": agent.get("routing"),
                    "ts": agent.get("updated_at"),
                }));
            }
        }
        if let Ok(body) = self.get_json("/api/routing").await {
            return Ok(body);
        }
        let gui = self.fetch_gui_status().await?;
        Ok(json!({
            "source": "derived",
            "unified_execution_route": gui.get("unified_execution_route"),
            "strategy_controller_decisions": gui.get("strategy_controller_decisions"),
            "hard_enforcement_decisions": gui.get("hard_enforcement_decisions"),
            "ts": gui.get("ts"),
        }))
    }

    pub async fn fetch_risk(&self) -> Result<Value, String> {
        if let Ok(agent) = self.fetch_agent_state().await {
            if agent.get("risk_envelope").is_some() || agent.get("governance_flags").is_some() {
                return Ok(json!({
                    "source": "agent_state",
                    "regime_risk_envelope": agent.get("risk_envelope"),
                    "governance_flags": agent.get("governance_flags"),
                    "ts": agent.get("updated_at"),
                }));
            }
        }
        if let Ok(body) = self.get_json("/api/risk").await {
            return Ok(body);
        }
        let gui = self.fetch_gui_status().await?;
        Ok(json!({
            "source": "derived",
            "regime_risk_envelope": gui.get("regime_risk_envelope"),
            "regime_sizing_advice": gui.get("regime_sizing_advice"),
            "pipeline_governance": gui.get("pipeline_governance"),
            "session_governance": gui.get("session_governance"),
            "daily_pnl_targeting": gui.get("daily_pnl_targeting"),
            "hard_enforcement_decisions": gui.get("hard_enforcement_decisions"),
            "ts": gui.get("ts"),
        }))
    }

    pub async fn fetch_logs(&self) -> Result<Value, String> {
        if let Ok(body) = self.get_json("/api/logs").await {
            return Ok(body);
        }
        let signals = self.get_json("/api/signals?limit=50").await.unwrap_or(json!({"signals": []}));
        let state = self.get_json("/state").await.unwrap_or(json!({}));
        Ok(json!({
            "source": "derived",
            "signals": signals.get("signals").cloned().unwrap_or(json!([])),
            "errors": state.get("errors"),
            "health_summary": state.pointer("/health/summary"),
            "ts": state.get("ts"),
        }))
    }

    pub fn ws_state_url(&self) -> String {
        self.base_url
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            + "/ws/state"
    }

    pub fn ws_url(&self) -> String {
        self.base_url
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            + "/ws/stream"
    }
}

pub fn default_base_url() -> String {
    std::env::var("IG_AGENT_API_URL")
        .or_else(|_| std::env::var("VITE_IG_AGENT_API_URL"))
        .unwrap_or_else(|_| "http://127.0.0.1:8080".to_string())
}
