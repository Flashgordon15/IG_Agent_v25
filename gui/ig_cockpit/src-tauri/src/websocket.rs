use crate::backend::{BackendClient, default_base_url};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const WS_EVENT: &str = "ws-stream";
const WS_CONN_EVENT: &str = "ws-connection";
const MAX_BACKOFF_SECS: u64 = 30;
const BASE_BACKOFF_SECS: u64 = 1;
const HEARTBEAT_CHECK_SECS: u64 = 3;
const DEGRADED_AFTER_SECS: u64 = 15;
const DEAD_AFTER_SECS: u64 = 30;

fn emit_state(app: &AppHandle, state: &str, detail: Option<&str>) {
    let mut payload = json!({ "state": state, "connected": state == "connected" });
    if let Some(d) = detail {
        payload["error"] = json!(d);
    }
    let _ = app.emit(WS_CONN_EVENT, payload);
}

pub async fn run_stream_loop(app: AppHandle) {
    let client = BackendClient::new(default_base_url());
    let ws_url = client.ws_url();
    let mut backoff_secs = BASE_BACKOFF_SECS;

    loop {
        emit_state(&app, "reconnecting", None);

        match connect_async(&ws_url).await {
            Ok((mut ws, _)) => {
                backoff_secs = BASE_BACKOFF_SECS;
                emit_state(&app, "connected", None);
                let mut last_message = Instant::now();
                let mut heartbeat =
                    tokio::time::interval(Duration::from_secs(HEARTBEAT_CHECK_SECS));

                loop {
                    tokio::select! {
                        msg = ws.next() => {
                            match msg {
                                Some(Ok(Message::Text(text))) => {
                                    last_message = Instant::now();
                                    if let Ok(payload) = serde_json::from_str::<Value>(&text) {
                                        let _ = app.emit(WS_EVENT, payload);
                                    }
                                    emit_state(&app, "connected", None);
                                }
                                Some(Ok(Message::Binary(bytes))) => {
                                    last_message = Instant::now();
                                    if let Ok(payload) = serde_json::from_slice::<Value>(&bytes) {
                                        let _ = app.emit(WS_EVENT, payload);
                                    }
                                    emit_state(&app, "connected", None);
                                }
                                Some(Ok(Message::Ping(data))) => {
                                    let _ = ws.send(Message::Pong(data)).await;
                                }
                                Some(Ok(Message::Pong(_))) => {
                                    last_message = Instant::now();
                                }
                                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                                _ => {}
                            }
                        }
                        _ = heartbeat.tick() => {
                            let silent = last_message.elapsed().as_secs();
                            if silent >= DEAD_AFTER_SECS {
                                emit_state(&app, "disconnected", Some("heartbeat timeout"));
                                let _ = ws.close(None).await;
                                break;
                            } else if silent >= DEGRADED_AFTER_SECS {
                                emit_state(&app, "degraded", Some("no ticks"));
                            } else {
                                let _ = ws.send(Message::Ping(vec![])).await;
                            }
                        }
                    }
                }
            }
            Err(err) => {
                emit_state(&app, "disconnected", Some(&err.to_string()));
            }
        }

        emit_state(&app, "reconnecting", Some("backoff"));
        tokio::time::sleep(Duration::from_secs(backoff_secs)).await;
        backoff_secs = (backoff_secs * 2).min(MAX_BACKOFF_SECS);
    }
}
