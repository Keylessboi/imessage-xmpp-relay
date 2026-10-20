//! Headless Linux iMessage server.
//!
//! This crate owns everything that is *not* Apple protocol specific: the config
//! directory, the persisted server state, the frozen local HTTP API and the SSE
//! event bus. The Apple IDS side is behind the [`backend::Backend`] trait:
//!
//! * [`backend::StubBackend`] ships in this crate. It parses the config and
//!   serves the API, but never registers, so every registration-gated endpoint
//!   answers `503 {"error":"not registered"}`.
//! * The real implementation (login, APS, IDS register, send/receive) lives in
//!   the separate `imessage-server-rustpush` crate, which is intentionally not
//!   a workspace member because it needs the `rustpush` git submodules.
//!
//! The HTTP surface is FROZEN; see `api.rs`.

use std::sync::Arc;

use anyhow::Result;

use crate::backend::Backend;
use crate::config::Config;
use crate::events::EventBus;
use crate::state::StateStore;

pub mod api;
pub mod attach;
pub mod backend;
pub mod config;
pub mod events;
pub mod groups;
pub mod files;
pub mod hexutil;
pub mod state;

/// Boot the server: load the state file, build the backend, start it in the
/// background (so the API answers during a slow login) and serve the frozen API
/// until the process is signalled.
///
/// `make_backend` receives the loaded [`Config`], the [`StateStore`] and the
/// [`EventBus`] the backend must publish inbound messages on.
pub async fn run<F>(config: Config, make_backend: F) -> Result<()>
where
    F: FnOnce(Config, StateStore, EventBus) -> Result<Arc<dyn Backend>>,
{
    tracing::info!(
        data_dir = %config.data_dir.display(),
        listen = %config.listen,
        hardware_config = %config.hwconfig_source,
        apple_id = %config.gsa.user,
        "config loaded"
    );

    let listen = config.listen;
    let files = config.files.clone();
    let (max_upload, spool_dir) = match &files {
        Some(f) => (f.limits.max_file_bytes, f.dir.join("outbound")),
        None => (100 << 20, config.data_dir.join("attachments-out")),
    };
    let bus = EventBus::new();
    let store = StateStore::load(
        config.data_dir.join("state.plist"),
        config.gsa.user.clone(),
        bus.clone(),
    )?;
    tracing::info!(
        state_file = %store.path().display(),
        registered = store.registered(),
        handles = store.handles().len(),
        "state loaded"
    );

    let backend = make_backend(config, store.clone(), bus.clone())?;
    tracing::info!(backend = backend.name(), "starting backend");

    // Start the backend in the background: /health must answer even while a
    // login or a 2FA wait is in flight.
    let startup_backend = backend.clone();
    tokio::spawn(async move {
        if let Err(e) = startup_backend.start().await {
            tracing::error!("backend ({}) failed to start: {e}", startup_backend.name());
        }
    });

    // The public download listener runs on its own port with its own router;
    // the control API below is never reachable through it.
    if let Some(files) = files {
        let store = attach::Store::open(files.dir.clone(), files.limits.clone())?;
        let state = Arc::new(files::FilesState {
            store,
            signer: files::Signer::new(files.hmac_key.clone()),
        });
        let addr = files.listen;
        tokio::spawn(async move {
            if let Err(e) = files::serve(state, addr).await {
                tracing::error!("attachment listener failed: {e}");
            }
        });
    } else {
        tracing::info!("attachments disabled (IMESSAGE_FILE_HMAC_KEY_FILE not set)");
    }

    let app = api::router(Arc::new(
        api::AppState::new(store, backend, bus).with_uploads(max_upload, spool_dir),
    ));

    let listener = tokio::net::TcpListener::bind(listen).await?;
    tracing::info!("listening on http://{listen}");
    axum::serve(listener, app).await?;
    Ok(())
}
