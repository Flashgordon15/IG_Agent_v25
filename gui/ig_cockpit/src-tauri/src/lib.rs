mod backend;
mod websocket;

use backend::{BackendClient, default_base_url};
use serde_json::Value;
use std::sync::Arc;
use tauri::{Emitter, State};

struct AppState {
    client: Arc<BackendClient>,
}

#[tauri::command]
async fn get_agent_state(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_agent_state().await
}

#[tauri::command]
async fn get_gui_status(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_gui_status().await
}

#[tauri::command]
async fn get_pnl_data(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_pnl().await
}

#[tauri::command]
async fn get_routing_metrics(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_routing().await
}

#[tauri::command]
async fn get_risk_state(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_risk().await
}

#[tauri::command]
async fn get_logs(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_logs().await
}

#[tauri::command]
async fn get_health_light(state: State<'_, AppState>) -> Result<Value, String> {
    state.client.fetch_health_light().await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let client = Arc::new(BackendClient::new(default_base_url()));

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            client: Arc::clone(&client),
        })
        .invoke_handler(tauri::generate_handler![
            get_agent_state,
            get_gui_status,
            get_pnl_data,
            get_routing_metrics,
            get_risk_state,
            get_logs,
            get_health_light,
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                websocket::run_stream_loop(handle).await;
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
