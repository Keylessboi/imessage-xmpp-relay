//! `imessage-server-rustpush` - the real Apple IDS server.
//!
//! Same frozen HTTP API as `imessage-server`, but the backend performs Apple ID
//! login, opens the APS connection, registers the IDS users and runs the push
//! loop. See `backend.rs` for the protocol side and ../imessage-server/README.md
//! for config + endpoints.

mod backend;
mod facetime_call;
mod sentlog;
mod sms2fa;

use std::sync::Arc;

use anyhow::Result;
use imessage_server::backend::Backend;
use imessage_server::config::Config;

use crate::backend::RustpushBackend;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    // rustls 0.23 links BOTH aws-lc-rs and ring through this dependency graph, so
    // it refuses to pick a process-level CryptoProvider and rustpush's APS setup
    // panics with "Could not automatically determine the process-level
    // CryptoProvider". Install one explicitly before anything touches TLS.
    // `ring` is what rustpush/crates/openbubbles-relay already use.
    if rustls::crypto::ring::default_provider()
        .install_default()
        .is_err()
    {
        tracing::debug!("a rustls CryptoProvider was already installed");
    }

    let config = Config::load()?;
    imessage_server::run(config, |config, store, bus| {
        Ok(Arc::new(RustpushBackend::new(config, store, bus)) as Arc<dyn Backend>)
    })
    .await
}
