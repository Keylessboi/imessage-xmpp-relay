//! Start a FaceTime call (a session that rings the contact) and hand back a
//! join link, on top of rustpush's `FTClient`.
//!
//! UNTESTED AGAINST APPLE: whether Apple accepts `create_session` / ringing from
//! the phone-only synthetic identity is unproven. The caller (`backend.rs`)
//! therefore falls back to a plain link and tells the owner nobody was rung.
//!
//! One long-lived `FTClient` serves both this and `POST /im/facetime-link`:
//! a call needs the APS push stream fed into `FTClient::handle` (QuickRelay
//! allocation replies arrive that way) and a local interface address, neither
//! of which the old build-per-request client had. The join link is secret, so
//! a let-me-in request (the owner opening the link in a browser) is approved
//! for the session this engine started last, and for nothing else. Link URLs
//! are never logged.

use std::net::{IpAddr, Ipv4Addr, UdpSocket};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use rustpush::facetime::{FTClient, FTMessage, FTState};
use rustpush::{APSConnection, IdentityManager, OSConfig};
use tokio::sync::{Mutex, OnceCell};
use uuid::Uuid;

/// Everything from the first APS round trip to the link must fit in this.
const CALL_TIMEOUT: Duration = Duration::from_secs(60);

static ENGINE: OnceCell<Arc<CallEngine>> = OnceCell::const_new();

pub struct CallEngine {
    ft: Arc<FTClient>,
    /// Group id of the session started last; the only one let-me-in may join.
    active: Mutex<Option<String>>,
}

/// The shared engine, built on first use. `identity` must be the IMClient's
/// IDS identity manager (same type `FTClient::new` takes).
pub async fn engine(
    state: FTState,
    persist: Box<dyn Fn(&FTState) + Send + Sync>,
    connection: APSConnection,
    identity: IdentityManager,
    config: Arc<dyn OSConfig>,
) -> Arc<CallEngine> {
    ENGINE
        .get_or_init(|| async move {
            let ft = Arc::new(FTClient::new(state, persist, connection.clone(), identity, config).await);
            ft.set_local_interfaces(&[local_ip()]).await;
            let engine = Arc::new(CallEngine { ft, active: Mutex::new(None) });
            tokio::spawn(pump(engine.clone(), connection));
            engine
        })
        .await
        .clone()
}

/// IDS form of a destination: `tel:+15551234567` or `mailto:a@b.c`.
pub fn ids_handle(to: &str) -> String {
    let t = to.trim();
    if let Some(rest) = t.strip_prefix("mailto:").or_else(|| t.strip_prefix("tel:")) {
        let kind = if t.starts_with("mailto:") { "mailto:" } else { "tel:" };
        return format!("{kind}{}", rest.trim());
    }
    if t.contains('@') {
        format!("mailto:{}", t.to_lowercase())
    } else {
        let digits: String = t.chars().filter(|c| c.is_ascii_digit()).collect();
        format!("tel:+{digits}")
    }
}

/// Best-effort outbound interface address (no packet is sent by `connect`).
fn local_ip() -> IpAddr {
    UdpSocket::bind("0.0.0.0:0")
        .and_then(|s| s.connect("17.0.0.1:9").map(|_| s))
        .and_then(|s| s.local_addr())
        .map(|a| a.ip())
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST))
}

impl CallEngine {
    pub fn ft(&self) -> &FTClient {
        &self.ft
    }

    /// Create a session inviting `peer` (IDS handle form), ring it, and return
    /// the session's join link.
    pub async fn place_call(&self, my_handle: &str, peer: &str, video: bool) -> Result<String> {
        let guid = Uuid::new_v4().to_string().to_uppercase();
        let work = async {
            self.ft
                .create_session(guid.clone(), my_handle.to_string(), &[peer.to_string()], video)
                .await
                .map_err(|e| anyhow!("creating the FaceTime session failed: {e}"))?;
            *self.active.lock().await = Some(guid.clone());
            // get_session_link panics on a missing session; check first.
            if !self.ft.state.read().await.sessions.contains_key(&guid) {
                return Err(anyhow!("the FaceTime session vanished after it was created"));
            }
            self.ft
                .get_session_link(&guid)
                .await
                .map_err(|e| anyhow!("getting the session link failed: {e}"))
        };
        tokio::time::timeout(CALL_TIMEOUT, work)
            .await
            .context("FaceTime call setup timed out")?
    }
}

/// Feed every APS message to the FaceTime client and approve the owner joining.
async fn pump(engine: Arc<CallEngine>, connection: APSConnection) {
    let mut sub = connection.messages_cont.subscribe();
    loop {
        let msg = match sub.recv().await {
            Ok(m) => m,
            Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                tracing::warn!("facetime push loop lagged by {n} messages");
                continue;
            }
            Err(tokio::sync::broadcast::error::RecvError::Closed) => return,
        };
        match engine.ft.handle(msg).await {
            Ok(Some(FTMessage::LetMeInRequest(req))) => {
                let active = engine.active.lock().await.clone();
                match active {
                    Some(guid) => {
                        tracing::info!("facetime: let-me-in request for the active session");
                        if let Err(e) = engine.ft.respond_letmein(req, Some(&guid)).await {
                            tracing::warn!("facetime: approving let-me-in failed: {e}");
                        }
                    }
                    None => tracing::info!("facetime: let-me-in request with no active session; ignored"),
                }
            }
            Ok(Some(other)) => tracing::debug!("facetime event: {}", event_kind(&other)),
            Ok(None) => {}
            Err(e) => tracing::debug!("facetime: push message not handled: {e}"),
        }
    }
}

fn event_kind(m: &FTMessage) -> &'static str {
    match m {
        FTMessage::LetMeInRequest(_) => "letmein",
        FTMessage::LinkChanged { .. } => "link_changed",
        FTMessage::JoinEvent { .. } => "join",
        FTMessage::AddMembers { .. } => "add_members",
        FTMessage::RemoveMembers { .. } => "remove_members",
        FTMessage::LeaveEvent { .. } => "leave",
        FTMessage::Ring { .. } => "ring",
        FTMessage::Decline { .. } => "decline",
        FTMessage::RespondedElsewhere { .. } => "responded_elsewhere",
        FTMessage::Connected { .. } => "connected",
        FTMessage::Disconnected { .. } => "disconnected",
    }
}

#[cfg(test)]
mod tests {
    use super::ids_handle;

    #[test]
    fn handles_map_to_ids_form() {
        assert_eq!(ids_handle("+1 (555) 765-4321"), "tel:+15557654321");
        assert_eq!(ids_handle("tel:+15557654321"), "tel:+15557654321");
        assert_eq!(ids_handle("Alice@Example.com"), "mailto:alice@example.com");
        assert_eq!(ids_handle("mailto:a@b.c"), "mailto:a@b.c");
    }
}
