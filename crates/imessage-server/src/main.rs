//! `imessage-server` - boots the config, the state file and the frozen HTTP API.
//!
//! This binary ships the STUB backend, so it compiles and runs with no Apple
//! account: config is parsed, the API is served, and registration-gated
//! endpoints answer `503 {"error":"not registered"}`.
//!
//! The real Apple IDS flow lives in `crates/imessage-server-rustpush` (a
//! separate, non-default crate - see the README).

use std::sync::Arc;

use anyhow::Result;
use imessage_server::backend::{Backend, StubBackend};
use imessage_server::config::Config;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let config = Config::load()?;
    imessage_server::run(config, |_config, store, _bus| {
        Ok(Arc::new(StubBackend::new(store)) as Arc<dyn Backend>)
    })
    .await
}
