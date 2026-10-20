//! Self-hosted OpenBubbles registration relay.
//!
//! Speaks the two endpoints the OpenBubbles client actually uses
//! (see rustpush/src/relay.rs:147 and hw_inp.dart:123):
//!
//!   POST /api/v1/bridge/get-version-info     -> {"versions": {...}}
//!   POST /api/v1/bridge/get-validation-data  -> {"data": "<base64>"} | 404
//!
//! Auth is `Authorization: Bearer <code>`; the client's relay host is
//! user-configurable in Settings, so pointing it here replaces
//! registration-relay.beeper.com entirely.

mod profile;

use std::collections::HashMap;
use std::sync::Arc;

use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{extract::State, Json, Router};
use base64::Engine;
use profile::{Profile, Store};
use serde_json::{json, Value};

#[derive(Clone)]
struct AppState {
    profiles: Arc<HashMap<String, Profile>>,
}

fn code_from(headers: &HeaderMap) -> Option<String> {
    headers
        .get("authorization")?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

type ApiError = (StatusCode, Json<Value>);

fn err(code: StatusCode, msg: &str) -> ApiError {
    (code, Json(json!({ "error": msg })))
}

async fn get_version_info(
    State(st): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let code = code_from(&headers).ok_or_else(|| err(StatusCode::UNAUTHORIZED, "missing code"))?;
    let p = st
        .profiles
        .get(&code)
        .ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown code"))?;

    Ok(Json(json!({
        "versions": {
            "software_build_id": p.software_build_id,
            "software_name": p.software_name,
            "software_version": p.software_version,
            "serial_number": p.hw.platform_serial_number,
            "hardware_version": p.hardware_version,
            "unique_device_id": p.unique_device_id,
        }
    })))
}

async fn get_validation_data(
    State(st): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let code = code_from(&headers).ok_or_else(|| err(StatusCode::UNAUTHORIZED, "missing code"))?;
    let p = st
        .profiles
        .get(&code)
        .ok_or_else(|| err(StatusCode::NOT_FOUND, "unknown code"))?
        .clone();

    // The emulator is synchronous and single-threaded; keep it off the reactor.
    let data = tokio::task::spawn_blocking(move || profile::acquire_validation_data(&p))
        .await
        .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, &format!("join: {e}")))?
        .map_err(|e| {
            tracing::error!("validation data failed: {e:#}");
            err(StatusCode::BAD_GATEWAY, &format!("{e}"))
        })?;

    let b64 = base64::engine::general_purpose::STANDARD.encode(&data);
    Ok(Json(json!({ "data": b64 })))
}

async fn health(State(st): State<AppState>) -> Json<Value> {
    Json(json!({ "ok": true, "profiles": st.profiles.len() }))
}

/// \`openbubbles-relay emit-hwconfig <CODE> [out.plist]\`
///
/// Writes the rustpush \`MacOSConfig\` for one profile. This is the file the
/// headless imessage-server boots from, so the server signs and registers with
/// exactly the identity this relay produces validation data for.
fn emit_hwconfig(store: &Store, code: &str, out: Option<&str>) -> anyhow::Result<()> {
    let p = store
        .profiles
        .get(code)
        .ok_or_else(|| anyhow::anyhow!("unknown profile code {code:?}"))?;
    let val = p.macos_config_plist();
    match out {
        Some(path) => {
            let mut f = std::fs::File::create(path)?;
            plist::to_writer_xml(&mut f, &val)?;
            // The profile is a device identity: keep it owner-only.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
            }
            tracing::info!("wrote {path} (profile {code}, mode 0600)");
        }
        None => plist::to_writer_xml(&mut std::io::stdout(), &val)?,
    }
    Ok(())
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let path = std::env::var("PROFILES_FILE").unwrap_or_else(|_| "profiles.json".into());
    let raw = std::fs::read_to_string(&path)?;
    let store: Store = serde_json::from_str(&raw)?;
    tracing::info!("loaded {} profile(s) from {path}", store.profiles.len());

    // One-shot mode: dump the rustpush MacOSConfig for a profile and exit.
    //   openbubbles-relay emit-hwconfig TESTCODE /data/hwconfig.plist
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("emit-hwconfig") {
        let code = args
            .get(1)
            .ok_or_else(|| anyhow::anyhow!("usage: openbubbles-relay emit-hwconfig <CODE> [out.plist]"))?;
        return emit_hwconfig(&store, code, args.get(2).map(String::as_str));
    }

    let state = AppState {
        profiles: Arc::new(store.profiles),
    };

    let app = Router::new()
        .route("/health", get(health))
        .route("/api/v1/bridge/get-version-info", post(get_version_info))
        .route("/api/v1/bridge/get-validation-data", post(get_validation_data))
        .with_state(state);

    let addr = std::env::var("LISTEN").unwrap_or_else(|_| "0.0.0.0:8787".into());
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!("listening on {addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
