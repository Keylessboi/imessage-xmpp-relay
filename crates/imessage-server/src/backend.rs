//! The Apple IDS backend boundary.
//!
//! The HTTP layer never talks to Apple directly. It gates on
//! [`StateStore::registered`](crate::state::StateStore::registered) and then
//! calls one of these four methods, so swapping in the real rustpush backend
//! cannot change the frozen API's behaviour.

use async_trait::async_trait;
use axum::http::StatusCode;

use crate::state::StateStore;

/// Login diagnostics surfaced on `GET /im/status`.
///
/// The HTTP layer is deliberately generic: it asks the backend for this and
/// serialises it. A backend that does not log in (the stub) leaves the default,
/// which is `{"login_error":null,"last_login_attempt":null}`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoginStatus {
    /// Last Apple ID / IDS login failure, or `None` if the last attempt
    /// succeeded or no attempt has run yet.
    pub error: Option<String>,
    /// Unix seconds of the most recent login attempt, or `None`.
    pub last_attempt: Option<i64>,
    /// True while the backend is blocked waiting for a 2FA code (the SMS read
    /// off the handset over adb). Lets `GET /im/status` say that the login is
    /// parked on a code rather than silently stuck.
    pub two_factor_pending: bool,
}

/// Error carrying the exact HTTP status + `{"error": ...}` body to return.
#[derive(Debug, Clone)]
pub struct BackendError {
    pub status: StatusCode,
    pub message: String,
}

impl BackendError {
    pub fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
    }

    pub fn not_registered() -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "not registered")
    }

    pub fn unavailable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, message)
    }

    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, message)
    }
}

impl std::fmt::Display for BackendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} ({})", self.message, self.status.as_u16())
    }
}

impl std::error::Error for BackendError {}

#[async_trait]
pub trait Backend: Send + Sync + 'static {
    /// Short name for logs ("stub", "rustpush").
    fn name(&self) -> &'static str;

    /// Boot the backend: Apple ID login, APS connection, IDS registration, and
    /// (in the real backend) the long-running push loop. Called once, from a
    /// spawned task, so the HTTP API is reachable while this is in flight.
    async fn start(&self) -> Result<(), BackendError>;

    /// Send a text message. Returns the message GUID. Only called when
    /// registered.
    async fn send_message(
        &self,
        to: &str,
        text: &str,
        reply_to: Option<String>,
    ) -> Result<String, BackendError>;

    /// Send a text into an existing group conversation, identified by the
    /// `chat_guid` an inbound `/im/events` message carried. Returns the message
    /// GUID. Only called when registered. The default keeps every other backend
    /// (and the stub) honest: group sending needs conversation state only the
    /// real rustpush backend has.
    async fn send_group(
        &self,
        _chat_guid: &str,
        _text: &str,
        _reply_to: Option<String>,
    ) -> Result<String, BackendError> {
        Err(BackendError::unavailable(
            "this backend cannot send into a group chat",
        ))
    }

    /// Mint (or reuse) an Apple FaceTime join link from the registered IDS
    /// identity. Optional `handle` is context only. Default: unavailable.
    async fn facetime_link(&self, _handle: Option<&str>) -> Result<String, BackendError> {
        Err(BackendError::unavailable(
            "this backend cannot create FaceTime links",
        ))
    }

    /// Register the `P:<number>` IDS user from an SMS `REG-RESP` signature.
    /// Called when registered, or earlier when
    /// [`Backend::phone_registration_available`] reports true.
    async fn register_phone(&self, number: &str, signature_hex: &str) -> Result<(), BackendError>;

    /// APNs push token, hex, for building the `REG-REQ` SMS. Available as soon
    /// as APS is up, which is *before* IDS registration, so this deliberately
    /// does not require `registered`.
    async fn apns_token(&self) -> Result<String, BackendError>;

    /// Login diagnostics for `GET /im/status`. Synchronous (it only reads a
    /// snapshot), and defaulted to "no login attempted" for backends that never
    /// log in, so the stub reports `login_error:null` and
    /// `last_login_attempt:null` without any extra code.
    fn login_status(&self) -> LoginStatus {
        LoginStatus::default()
    }

    /// Whether `register_phone` may run before IDS registration completes
    /// (`/im/status` still says `registered:false`).
    ///
    /// The real rustpush backend mints a `P:<number>` IDS user from the APNs
    /// push token plus the carrier signature alone, so it answers `true` even
    /// when no Apple ID is logged in and the HTTP layer would otherwise gate the
    /// request on `registered`. The stub cannot, so the default keeps the
    /// `503 {"error":"not registered"}` behaviour for every other backend.
    fn phone_registration_available(&self) -> bool {
        false
    }
}

/// Build-only backend: the config is parsed and the API is served, but no Apple
/// account is touched. Every registration-gated endpoint returns 503.
pub struct StubBackend {
    store: StateStore,
}

impl StubBackend {
    pub fn new(store: StateStore) -> Self {
        Self { store }
    }
}

#[async_trait]
impl Backend for StubBackend {
    fn name(&self) -> &'static str {
        "stub"
    }

    async fn start(&self) -> Result<(), BackendError> {
        tracing::warn!(
            "using the STUB backend: config is loaded but no Apple ID login, APS connection or \
             IDS registration will happen. Build crates/imessage-server-rustpush and run that \
             binary for the real flow (see crates/imessage-server/README.md)."
        );
        // A state file written by a real backend may still say "registered".
        // This binary cannot send or receive, so report the truth.
        if self.store.registered() {
            tracing::warn!(
                "clearing the 'registered' flag: the stub backend cannot stay registered"
            );
            self.store.set_registered(false, Vec::new());
        }
        Ok(())
    }

    async fn send_message(
        &self,
        _to: &str,
        _text: &str,
        _reply_to: Option<String>,
    ) -> Result<String, BackendError> {
        Err(BackendError::unavailable(
            "this build has the stub backend and cannot send messages",
        ))
    }

    async fn register_phone(
        &self,
        _number: &str,
        _signature_hex: &str,
    ) -> Result<(), BackendError> {
        Err(BackendError::unavailable(
            "this build has the stub backend and cannot register phone numbers",
        ))
    }

    async fn apns_token(&self) -> Result<String, BackendError> {
        Err(BackendError::unavailable(
            "no APNs token: this build has the stub backend",
        ))
    }
}
