//! Apple IDS backend implemented on rustpush.
//!
//! STRUCTURE MIRRORS imessage-lab/rustpush/src/test.rs (the only known-good
//! reference for this code path):
//!
//!   MacOSConfig (from hwconfig.plist)
//!     -> APSConnectionResource::new(config, saved APSState)
//!     -> anisette client (ANISETTE_URL; default https://ani.sidestore.io)
//!     -> AppleAccount::login(...)  [first boot only]
//!     -> login_apple_delegates(IDSMobileMe) -> authenticate_apple -> IDSUser
//!     -> register(...)                      [first boot only]
//!     -> IMClient::new(...) + push loop -> SSE
//!
//! DEGRADED MODE
//! =============
//! The Apple ID step (GSA SRP) and the phone-number step (carrier REG-RESP
//! signature) are independent: authenticate_phone talks to IDS with only the
//! device config and the APNs push token, never an Apple ID. So a failed or
//! absent Apple ID login must not stop the process:
//!
//!   * start_inner opens APS, publishes the APNs token and installs the
//!     backend as RUNNING with no IMClient; the HTTP server keeps serving.
//!   * GET /apns-token answers from the live APS connection.
//!   * POST /im/register-phone works with no prior login.
//!   * A failed Apple ID login is logged clearly and retried on a slow bounded
//!     backoff (IMESSAGE_LOGIN_RETRY_SECS, default 1800 = 30 min, doubling to a
//!     3600 s cap; 0 disables) instead of being fatal at boot.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use axum::http::StatusCode;
use plist::Data;
use rustpush::macos::MacOSConfig;
// rustpush::aps is a private module; APSConnection is re-exported at the crate
// root (rustpush/src/lib.rs:43). Importing the module path fails with E0603 and
// then cascades into bogus "unsized [u8]" errors on every .state.read().token.
use rustpush::{
    authenticate_apple, authenticate_phone, login_apple_delegates, register, APSConnection,
    APSConnectionResource, APSState, AppleAccount, ArcAnisetteClient, AuthPhone, ConversationData,
    DefaultAnisetteProvider, IDSNGMIdentity, IDSUser, IMClient, LoginClientInfo, LoginDelegate,
    Message, MessageInst, MessageType, NormalMessage, OSConfig, RelayConfig, MADRID_SERVICE,
};

use imessage_server::backend::{Backend, BackendError, LoginStatus};
use imessage_server::config::Config;
use imessage_server::events::{
    chat_guid_for, normalize_handle, strip_handle, EventBus, MessageEvent,
};
use imessage_server::hexutil;
use imessage_server::state::StateStore;

/// How long /im/register-phone and the login path wait for a 2FA code.
const TWO_FACTOR_TIMEOUT_SECS: u64 = 600;

/// Base delay before the first Apple ID login retry, and the first step of the
/// backoff. 30 minutes: Apple's \`-80045\` block is ecosystem-wide and expected to
/// lift on its own, so retrying faster only adds load and log noise.
const DEFAULT_LOGIN_RETRY_SECS: u64 = 1800;

/// Ceiling for the login-retry backoff: never wait longer than 60 minutes
/// between attempts, so a lifted block is noticed within the hour.
const LOGIN_RETRY_MAX_SECS: u64 = 3600;

/// Anisette-v3 backend used unless the `ANISETTE_URL` override is set. Kept in
/// sync with `omnisette::DEFAULT_ANISETTE_URL_V3`.
const DEFAULT_ANISETTE_URL: &str = "https://ani.sidestore.io";

/// Base URL for the anisette-v3 backend: `ANISETTE_URL`, else
/// `OMNISETTE_URL_V3` (the name the omnisette fork patch also honours, so both
/// knobs agree), else `DEFAULT_ANISETTE_URL`. Trailing slashes are tolerated.
fn anisette_url() -> String {
    for key in ["ANISETTE_URL", "OMNISETTE_URL_V3"] {
        if let Some(url) = std::env::var(key)
            .ok()
            .map(|raw| raw.trim().trim_end_matches('/').to_string())
            .filter(|raw| !raw.is_empty())
        {
            return url;
        }
    }
    DEFAULT_ANISETTE_URL.to_string()
}

/// Build the rustpush device config from the self-hosted registration relay.
///
/// The relay bridges to a jailbroken iPhone running ValidationRelay: it answers
/// `get-version-info` with that phone's real version tuple and
/// `get-validation-data` with its genuine NAC blob, so IDS/APS see an iPhone
/// instead of the Mac identity seeded into hwconfig.plist.
///
/// Returns `None` when `REGISTRATION_RELAY_URL`/`REGISTRATION_RELAY_CODE` are
/// not set (the MacOSConfig fallback) or when the relay cannot be reached.
async fn relay_config() -> Option<Arc<dyn OSConfig>> {
    let host = std::env::var("REGISTRATION_RELAY_URL")
        .ok()
        .map(|raw| raw.trim().trim_end_matches('/').to_string())
        .filter(|raw| !raw.is_empty())?;
    let code = match std::env::var("REGISTRATION_RELAY_CODE") {
        Ok(raw) if !raw.trim().is_empty() => raw.trim().to_string(),
        _ => {
            tracing::error!(
                "REGISTRATION_RELAY_URL is set but REGISTRATION_RELAY_CODE is not; \
                 falling back to the Mac identity"
            );
            return None;
        }
    };

    let beeper_token: Option<String> = None;
    let version = match RelayConfig::get_versions(&host, &code, &beeper_token).await {
        Ok(version) => version,
        Err(e) => {
            tracing::error!(
                "registration relay {host} did not answer get-version-info: {e}; \
                 falling back to the Mac identity"
            );
            return None;
        }
    };

    // Versions keeps its fields private, so read the values back through a
    // plist round-trip. unique_device_id is this device's UDID (X-Client-UDID);
    // a fresh one is only a last-resort fallback if that shape ever changes.
    let info = plist::to_value(&version).ok().and_then(|value| value.into_dictionary());
    let field = |key: &str| {
        info.as_ref()
            .and_then(|dict| dict.get(key))
            .and_then(|value| value.as_string())
            .unwrap_or("")
            .to_string()
    };
    let udid = match field("unique_device_id") {
        value if !value.is_empty() => value,
        _ => uuid::Uuid::new_v4().to_string().to_uppercase(),
    };

    tracing::info!(
        "device identity: registration relay {host} -> {} {} (build {}), serial {}, udid {}",
        field("hardware_version"),
        field("software_version"),
        field("software_build_id"),
        field("serial_number"),
        udid,
    );

    Some(Arc::new(RelayConfig {
        version,
        icloud_ua: "com.apple.iCloudHelper/282 CFNetwork/1408.0.4 Darwin/22.5.0".to_string(),
        aoskit_version: "com.apple.AOSKit/282 (com.apple.accountsd/113)".to_string(),
        dev_uuid: uuid::Uuid::new_v4().to_string(),
        protocol_version: 1640,
        host,
        code,
        beeper_token: None,
        udid: Some(udid),
    }))
}

/// Build the anisette provider with a configurable URL.
///
/// `rustpush::default_provider` hardcodes `omnisette::DEFAULT_ANISETTE_URL_V3`
/// (omnisette/src/lib.rs), so upstream offers no config knob for it. This
/// constructs exactly the same provider + on-disk state, only taking the base
/// URL from `ANISETTE_URL` first (trailing slashes tolerated). The provider
/// type, the `<data_dir>/anisette/state.plist` location and the
/// `AppleAccount::login` call are all identical to `default_provider`.
fn make_anisette_provider(
    info: LoginClientInfo,
    path: PathBuf,
) -> ArcAnisetteClient<DefaultAnisetteProvider> {
    let url = anisette_url();
    tracing::info!("anisette backend: {url} (override with ANISETTE_URL / OMNISETTE_URL_V3)");
    Arc::new(tokio::sync::Mutex::new(omnisette::AnisetteClient::new(
        omnisette::remote_anisette_v3::RemoteAnisetteProviderV3::new(url, info, path),
    )))
}

/// The IDS services registered with Apple, matching rustpush/src/test.rs.
/// A macro because rustpush does not re-export the IDSService type, so the
/// array type cannot be named at a function boundary.
macro_rules! id_services {
    () => {
        &[
            &MADRID_SERVICE,
            &rustpush::findmy::MULTIPLEX_SERVICE,
            &rustpush::facetime::FACETIME_SERVICE,
            &rustpush::facetime::VIDEO_SERVICE,
        ]
    };
}

/// The durable half of the backend, stored in state.plist.backend_state.
// No Debug: APSState, IDSUser and IDSNGMIdentity do not implement it, and this
// struct is persisted to state.plist rather than logged.
#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
struct SavedBackend {
    /// rustpush push state (APNs token, APNs keypair).
    #[serde(default)]
    push: Option<APSState>,
    /// Authenticated IDS users: mailto:<apple id> plus any P:<number>.
    #[serde(default)]
    users: Vec<IDSUser>,
    /// The NGM identity created on first registration.
    #[serde(default)]
    identity: Option<IDSNGMIdentity>,
    /// FaceTime link/session state (rustpush `FTState`), serialised as a plist
    /// blob. Blob rather than a typed field because `FTState` is not `Clone`
    /// and rustpush's update callback only hands out `&FTState`. Persisting it
    /// is what keeps a minted link (and its pseudonym) valid across restarts,
    /// the same way `push` keeps the APNs token stable.
    #[serde(default)]
    facetime: Option<Data>,
    /// Conversations seen inbound, keyed by `chat_guid`, so
    /// `POST /im/send {"chat_guid":...}` can reply into an existing group.
    /// Recorded from the inbound wire message (member list + iMessage group
    /// id); it is the only group state rustpush gives us.
    #[serde(default)]
    chats: HashMap<String, SavedChat>,
}

/// One conversation learned from an inbound message.
#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
struct SavedChat {
    /// Full participant set from the inbound message, `tel:`/`mailto:`
    /// prefixed so it can go straight back out as IDS participants.
    #[serde(default)]
    participants: Vec<String>,
    /// iMessage group id (the wire `gid`): the id every member's device keys
    /// the thread on. Reusing it is what makes a reply land in the existing
    /// thread rather than starting a new one.
    #[serde(default)]
    sender_guid: Option<String>,
    /// Unix ms of the last inbound message, used only for eviction.
    #[serde(default)]
    updated_ms: i64,
}

/// Shared, cheap-to-clone conversation map.
type Chats = Arc<RwLock<HashMap<String, SavedChat>>>;

/// How many conversations are remembered before the oldest is evicted.
const MAX_CHATS: usize = 512;

/// The single FaceTime link usage string this server mints. A FaceTime link is
/// a shareable join link, not a per-recipient token, so one usage means repeat
/// calls return the SAME link instead of minting a new one each time.
const FACETIME_LINK_USAGE: &str = "relay-link";

/// Everything that only exists while the process is running.
///
/// The APS connection is available from first boot; client only appears once at
/// least one IDS user is registered with Apple (Apple ID or phone number).
struct Running {
    connection: APSConnection,
    config: Arc<dyn OSConfig>,
    identity: IDSNGMIdentity,
    /// Authenticated IDS users: mailto:<apple id> and/or P:<number>.
    users: Vec<IDSUser>,
    /// Built once the users above are registered with Apple.
    client: Option<Arc<IMClient>>,
}

pub struct RustpushBackend {
    config: Config,
    store: StateStore,
    bus: EventBus,
    running: Arc<tokio::sync::RwLock<Option<Running>>>,
    /// Bumped whenever the IMClient is rebuilt, so the old push loop exits.
    generation: Arc<AtomicU64>,
    /// Login diagnostics surfaced on GET /im/status. Read by the HTTP handler,
    /// written by the boot/retry login tasks; a std lock because the read is a
    /// cheap synchronous snapshot.
    login: Arc<RwLock<LoginStatus>>,
    /// Serialises /im/register-phone so two concurrent registrations cannot
    /// clobber each other's user list.
    register_lock: tokio::sync::Mutex<()>,
    /// Inbound conversations, so /im/send can reply into a group. Loaded from
    /// state.plist at boot and written back by the push loop.
    chats: Chats,
}

impl RustpushBackend {
    pub fn new(config: Config, store: StateStore, bus: EventBus) -> Self {
        // Chats live in the same opaque backend blob as the IDS state; load them
        // before the store moves into the struct.
        let saved: SavedBackend = store
            .backend_state()
            .and_then(|blob| plist::from_bytes(blob.as_ref()).ok())
            .unwrap_or_default();
        Self {
            config,
            store,
            bus,
            running: Arc::new(tokio::sync::RwLock::new(None)),
            generation: Arc::new(AtomicU64::new(0)),
            login: Arc::new(RwLock::new(LoginStatus::default())),
            register_lock: tokio::sync::Mutex::new(()),
            chats: Arc::new(RwLock::new(saved.chats)),
        }
    }

    async fn start_inner(&self) -> Result<()> {
        let data_dir = self.config.data_dir.clone();
        std::fs::create_dir_all(&data_dir)
            .with_context(|| format!("creating data dir {}", data_dir.display()))?;

        init_software_keystore(&data_dir)?;

        // Device identity: the self-hosted registration relay (a real iPhone's
        // version tuple + NAC validation data) when REGISTRATION_RELAY_URL and
        // REGISTRATION_RELAY_CODE are set, else the Mac identity from
        // hwconfig.plist. The relay path makes IDS see an iPhone10,6 rather than
        // the Mac profile, which is what the jailbroken-X bridge is for.
        let macos_config: Arc<dyn OSConfig> = match relay_config().await {
            Some(relay) => relay,
            None => Arc::new(
                plist::from_bytes::<MacOSConfig>(&self.config.hwconfig_raw).context(
                    "hwconfig.plist is not a rustpush MacOSConfig (extract it from a Mac's \
                     validation data with HardwareConfig::from_validation_data)",
                )?,
            ),
        };

        let saved: SavedBackend = match self.store.backend_state() {
            Some(blob) => plist::from_bytes(blob.as_ref()).unwrap_or_default(),
            None => SavedBackend::default(),
        };

        // The APS connection reconnects on its own; an error here only means the
        // push token is not ready yet.
        let (connection, aps_error) =
            APSConnectionResource::new(macos_config.clone(), saved.push.clone()).await;
        if let Some(error) = aps_error {
            tracing::warn!("APS reported an error during startup: {error}");
        }

        // Publish the token and keep watching until APS has one: the phone path
        // only needs this, not the Apple ID.
        //
        // Save the APS state (keypair + token) HERE, before the Apple ID login
        // is even attempted. install_registered below is the only other writer
        // of it, and it is never reached on a degraded boot - which is exactly
        // when the state must still be durable: without a saved keypair APS
        // activates a new one (and mints a new APNs token) on every restart,
        // and an APNs token is bound to one REG-REQ/REG-RESP signature pair.
        let token_already_present = connection.state.read().await.token.is_some();
        publish_apns_token(&self.store, &connection).await;
        persist_aps_state(&self.store, &connection).await;
        // Only watch when APS has no token yet; otherwise the spawn would
        // immediately re-persist the same state it already saved above.
        if !token_already_present {
            spawn_apns_token_watch(self.store.clone(), connection.clone());
        }

        let anisette = make_anisette_provider(
            macos_config.get_gsa_config(&*connection.state.read().await, false),
            data_dir.join("anisette"),
        );

        let identity = match saved.identity.clone() {
            Some(identity) => identity,
            None => IDSNGMIdentity::new().map_err(|e| anyhow!("IDSNGMIdentity::new: {e}"))?,
        };

        // Come up RUNNING even before (and in case of) the Apple ID login, so
        // /health and /apns-token answer while anything slow is in flight.
        install_degraded(
            &self.store,
            &self.running,
            connection.clone(),
            macos_config.clone(),
            saved.users.clone(),
            identity.clone(),
        )
        .await;

        if saved.users.is_empty() {
            tracing::info!(
                "no saved IDS users: an Apple ID login is required before /im/send can work; \
                 the phone-number path (POST /im/register-phone) does not need it"
            );
        }

        let cache_path = data_dir.join("id_cache.plist");
        let login = LoginState {
            data_dir,
            cache_path,
            store: self.store.clone(),
            bus: self.bus.clone(),
            running: self.running.clone(),
            generation: self.generation.clone(),
            login: self.login.clone(),
            connection,
            config: macos_config,
            identity,
            anisette,
            gsa_user: self.config.gsa.user.clone(),
            gsa_pass: self.config.gsa.pass_sha256.clone(),
            chats: self.chats.clone(),
        };

        // Run the login in its own task. rustpush's remote anisette provider
        // panics on some upstream errors (omnisette remote_anisette_v3.rs:419
        // does a bare panic!() instead of returning the error), and that panic
        // must not abort start_inner: a JoinError is just another "the login did
        // not complete" for us, and the retry below still gets scheduled.
        let attempt = match tokio::spawn(complete_apple_id(login.clone())).await {
            Ok(result) => result,
            Err(join_error) => Err(anyhow!("Apple ID login task panicked: {join_error}")),
        };

        match attempt {
            Ok(()) => Ok(()),
            Err(e) => {
                tracing::error!("Apple ID / IDS registration did not complete: {e:#}");
                tracing::warn!(
                    "running DEGRADED (registered=false): the HTTP server stays up, \
                     GET /apns-token works and POST /im/register-phone works without an \
                     Apple ID; /im/send is unavailable until login succeeds"
                );

                let retry_secs = login_retry_secs();
                if retry_secs == 0 {
                    tracing::warn!(
                        "Apple ID login retry is disabled (IMESSAGE_LOGIN_RETRY_SECS=0)"
                    );
                } else {
                    tracing::info!(
                        "will retry the Apple ID login with a bounded backoff starting at \
                         {retry_secs}s and capping at {LOGIN_RETRY_MAX_SECS}s \
                         (set IMESSAGE_LOGIN_RETRY_SECS=0 to disable)"
                    );
                    tokio::spawn(retry_login_loop(login, Duration::from_secs(retry_secs)));
                }
                Ok(())
            }
        }
    }
    /// One send path for 1:1 and group: a ConversationData plus text. The
    /// participants are whatever the caller resolved (a peer handle, or the
    /// member set recorded for a group).
    async fn send_with(
        &self,
        conversation: ConversationData,
        text: &str,
        reply_to: Option<String>,
    ) -> Result<String> {
        let guard = self.running.read().await;
        let running = guard
            .as_ref()
            .ok_or_else(|| anyhow!("backend is not running yet"))?;
        let client = running.client.as_ref().ok_or_else(|| {
            anyhow!(
                "no IDS user is registered with Apple yet (Apple ID login failed or no phone \
                 number registered)"
            )
        })?;

        let handles = client.identity.get_handles().await;
        let sender = handles
            .first()
            .cloned()
            .ok_or_else(|| anyhow!("no IDS handle is registered"))?;

        let (reply_guid, reply_part) = split_reply(reply_to);
        let mut normal = NormalMessage::new(text.to_string(), MessageType::IMessage);
        normal.reply_guid = reply_guid;
        normal.reply_part = reply_part;

        let mut inst = MessageInst::new(conversation, &sender, Message::Message(normal));

        let guid = inst.id.clone();
        client
            .send(&mut inst)
            .await
            .map_err(|e| anyhow!("send failed: {e}"))?;
        Ok(guid)
    }

    /// Send a 1:1 text. Unchanged wire shape: one peer participant and a fresh
    /// conversation guid, exactly as before group sending existed.
    async fn send_inner(&self, to: &str, text: &str, reply_to: Option<String>) -> Result<String> {
        self.send_with(
            ConversationData {
                participants: vec![imessage_server::events::normalize_handle(to)],
                cv_name: None,
                sender_guid: Some(uuid::Uuid::new_v4().to_string()),
                after_guid: None,
            },
            text,
            reply_to,
        )
        .await
    }

    /// Send into an existing group: the member list and the iMessage group id
    /// come from the last inbound message for this chat_guid. Reusing the group
    /// id is what puts the message in the same thread on every member's device;
    /// a fresh id would create a new thread with the same people.
    async fn send_group_inner(
        &self,
        chat_guid: &str,
        text: &str,
        reply_to: Option<String>,
    ) -> Result<String> {
        let chat = self
            .chats
            .read()
            .map_err(|_| anyhow!("conversation map lock poisoned"))?
            .get(chat_guid)
            .cloned()
            .ok_or_else(|| {
                anyhow!(
                    "unknown chat_guid `{chat_guid}`: no inbound message for that conversation \
                     has been seen (group replies need the member list and group id from an \
                     inbound message, which is the only group state rustpush provides)"
                )
            })?;

        let sender_guid = chat.sender_guid.clone().ok_or_else(|| {
            anyhow!(
                "chat `{chat_guid}` has no iMessage group id (the inbound message carried no \
                 `gid`), so there is no thread to reply into"
            )
        })?;

        let conversation = group_conversation(&chat, sender_guid);
        self.send_with(conversation, text, reply_to).await
    }

    /// Mint (or reuse) a FaceTime join link from the registered IDS identity.
    ///
    /// rustpush builds the link from an IDS pseudonym (`create_pseudonym`,
    /// feature "Gondola", service com.apple.private.alloy.facetime.multi) that
    /// Apple provisions against one of *our own* handles, so the optional
    /// `handle` is only context: it selects which of our handles is the origin
    /// when it names one of them.
    async fn facetime_link_inner(&self, handle: Option<&str>) -> Result<String> {
        let guard = self.running.read().await;
        let running = guard
            .as_ref()
            .ok_or_else(|| anyhow!("backend is not running yet"))?;
        let client = running.client.as_ref().ok_or_else(|| {
            anyhow!("no IDS user is registered with Apple yet; a FaceTime link needs one")
        })?;

        let handles = client.identity.get_handles().await;
        let bare = handle.map(strip_handle).unwrap_or_default();
        let my_handle = handles
            .iter()
            .find(|candidate| strip_handle(candidate) == bare)
            .cloned()
            .or_else(|| handles.iter().find(|h| h.starts_with("tel:")).cloned())
            .or_else(|| handles.first().cloned())
            .ok_or_else(|| anyhow!("no IDS handle is registered"))?;

        // The saved FTState is what makes a previously minted link (and its
        // pseudonym) survive a restart; rustpush validates it before reuse and
        // mints a replacement when Apple says it is gone.
        let saved: SavedBackend = self
            .store
            .backend_state()
            .and_then(|blob| plist::from_bytes(blob.as_ref()).ok())
            .unwrap_or_default();
        let ft_state = saved
            .facetime
            .as_ref()
            .and_then(|blob| plist::from_bytes::<rustpush::facetime::FTState>(blob.as_ref()).ok())
            .unwrap_or_default();

        let store = self.store.clone();
        let facetime = rustpush::facetime::FTClient::new(
            ft_state,
            Box::new(move |state: &rustpush::facetime::FTState| persist_facetime(&store, state)),
            running.connection.clone(),
            client.identity.clone(),
            running.config.clone(),
        )
        .await;

        facetime
            .get_link_for_usage(&my_handle, FACETIME_LINK_USAGE)
            .await
            .map_err(|e| anyhow!("FaceTime link request for {my_handle} failed: {e}"))
    }
}

#[async_trait]
impl Backend for RustpushBackend {
    fn name(&self) -> &'static str {
        "rustpush"
    }

    async fn start(&self) -> Result<(), BackendError> {
        match self.start_inner().await {
            Ok(()) => Ok(()),
            Err(e) => {
                // Only genuinely fatal setup problems land here (bad data dir,
                // unparsable hwconfig, no NGM identity). A failed Apple ID login
                // is handled inside start_inner and leaves the backend running
                // in degraded mode.
                tracing::error!("rustpush backend failed to start: {e:#}");
                Err(BackendError::internal(format!("{e:#}")))
            }
        }
    }

    async fn send_message(
        &self,
        to: &str,
        text: &str,
        reply_to: Option<String>,
    ) -> Result<String, BackendError> {
        self.send_inner(to, text, reply_to)
            .await
            .map_err(|e| BackendError::unavailable(format!("{e:#}")))
    }

    async fn send_group(
        &self,
        chat_guid: &str,
        text: &str,
        reply_to: Option<String>,
    ) -> Result<String, BackendError> {
        self.send_group_inner(chat_guid, text, reply_to)
            .await
            .map_err(|e| BackendError::unavailable(format!("{e:#}")))
    }

    async fn facetime_link(&self, handle: Option<&str>) -> Result<String, BackendError> {
        self.facetime_link_inner(handle)
            .await
            // Apple's own error text travels intact in the message.
            .map_err(|e| BackendError::new(StatusCode::BAD_GATEWAY, format!("{e:#}")))
    }

    async fn register_phone(&self, number: &str, signature_hex: &str) -> Result<(), BackendError> {
        let signature = hexutil::decode_hex(signature_hex)
            .ok_or_else(|| BackendError::bad_request("signature is not valid hex"))?;
        if signature.is_empty() {
            return Err(BackendError::bad_request("signature is empty"));
        }

        // One registration at a time: both the user-list write and the IMClient
        // rebuild are read-modify-write.
        let _registration = self.register_lock.lock().await;

        let (connection, config, identity, mut users) = {
            let guard = self.running.read().await;
            let running = guard
                .as_ref()
                .ok_or_else(|| BackendError::unavailable("backend is not running yet"))?;
            (
                running.connection.clone(),
                running.config.clone(),
                running.identity.clone(),
                running.users.clone(),
            )
        };

        // Copy the token out before awaiting anything else: no guard lives
        // across the request to Apple.
        let push_token = connection
            .state
            .read()
            .await
            .token
            .ok_or_else(|| BackendError::unavailable("APS has no push token yet"))?;

        let user = authenticate_phone(
            number,
            AuthPhone {
                push_token: Data::new(push_token.to_vec()),
                sigs: vec![Data::new(signature)],
            },
            config.as_ref(),
        )
        .await
        .map_err(|e| {
            // Apple's own text (status/message), not a generic 500.
            BackendError::bad_request(format!("authenticate_phone failed: {e}"))
        })?;
        users.push(user);

        register(
            config.as_ref(),
            &*connection.state.read().await,
            id_services!(),
            &mut users,
            &identity,
        )
        .await
        .map_err(|e| {
            BackendError::new(
                StatusCode::BAD_GATEWAY,
                format!("IDS registration failed: {e}"),
            )
        })?;

        install_registered(
            &self.store,
            &self.bus,
            &self.running,
            &self.generation,
            connection,
            config,
            users,
            identity,
            self.config.data_dir.join("id_cache.plist"),
            self.chats.clone(),
        )
        .await;
        Ok(())
    }

    async fn apns_token(&self) -> Result<String, BackendError> {
        let guard = self.running.read().await;
        let running = guard
            .as_ref()
            .ok_or_else(|| BackendError::unavailable("APS is not connected yet"))?;
        // Copy the token out before matching: holding the read guard across the
        // match keeps a borrow of running (and so of guard) alive in the arm.
        let token = running.connection.state.read().await.token;
        match token {
            Some(token) => Ok(hexutil::encode_hex(&token)),
            None => Err(BackendError::unavailable("APS has no push token yet")),
        }
    }

    /// The phone path needs no Apple ID, so it is reachable in degraded mode.
    /// The backend itself reports Apple's error text when a token/signature is
    /// missing or rejected.
    fn phone_registration_available(&self) -> bool {
        true
    }

    /// Last Apple ID / IDS login result, so GET /im/status can show whether
    /// Apple has unblocked us without anyone scraping the log.
    fn login_status(&self) -> LoginStatus {
        self.login.read().map(|status| status.clone()).unwrap_or_default()
    }
}

// ------------------------------------------------------------------ helpers --

/// true while the backend has an IMClient (i.e. at least one IDS user is
/// registered with Apple).
async fn is_registered(running: &tokio::sync::RwLock<Option<Running>>) -> bool {
    running
        .read()
        .await
        .as_ref()
        .map(|running| running.client.is_some())
        .unwrap_or(false)
}

/// Install the backend as running without an IMClient: APS is up (or will
/// reconnect), but no IDS user is registered yet.
async fn install_degraded(
    store: &StateStore,
    running: &tokio::sync::RwLock<Option<Running>>,
    connection: APSConnection,
    config: Arc<dyn OSConfig>,
    users: Vec<IDSUser>,
    identity: IDSNGMIdentity,
) {
    let registered = users
        .iter()
        .any(|user| user.registration.contains_key("com.apple.madrid"));
    if registered {
        // Saved registration is about to be reused; keep the handles visible.
        store.set_handles(handles_from(&users));
    } else {
        // Be honest: nothing is registered on this boot, whatever the file said.
        store.set_registered(false, Vec::new());
    }

    *running.write().await = Some(Running {
        connection,
        config,
        identity,
        users,
        client: None,
    });
}

/// Build the IMClient from already-registered users, publish the APNs token and
/// handles, persist the durable state, and start the push loop.
#[allow(clippy::too_many_arguments)]
async fn install_registered(
    store: &StateStore,
    bus: &EventBus,
    running: &tokio::sync::RwLock<Option<Running>>,
    generation: &Arc<AtomicU64>,
    connection: APSConnection,
    config: Arc<dyn OSConfig>,
    users: Vec<IDSUser>,
    identity: IDSNGMIdentity,
    cache_path: PathBuf,
    chats: Chats,
) {
    let push_state = Some(connection.state.read().await.clone());
    let services = id_services!();
    let client = Arc::new(
        IMClient::new(
            connection.clone(),
            users.clone(),
            identity.clone(),
            services,
            cache_path,
            config.clone(),
            make_keys_updated(store.clone(), identity.clone(), push_state.clone()),
        )
        .await,
    );

    publish_apns_token(store, &connection).await;

    let handles = handles_from(&users);
    tracing::info!(?handles, "IDS registered");
    store.set_registered(true, handles);
    persist_backend(store, push_state, &users, &identity);

    let my_generation = generation.fetch_add(1, Ordering::SeqCst) + 1;
    let loop_client = client.clone();
    let loop_connection = connection.clone();
    let loop_bus = bus.clone();
    let loop_generation = generation.clone();
    let loop_chats = chats.clone();
    let loop_store = store.clone();
    tokio::spawn(async move {
        receive_loop(
            loop_connection,
            loop_client,
            loop_bus,
            loop_generation,
            my_generation,
            loop_chats,
            loop_store,
        )
        .await;
    });

    *running.write().await = Some(Running {
        connection,
        config,
        identity,
        users,
        client: Some(client),
    });
}

/// Everything the Apple ID login needs, owned: both the boot attempt and the
/// background retry task run off this, so neither borrows the backend.
#[derive(Clone)]
struct LoginState {
    data_dir: PathBuf,
    cache_path: PathBuf,
    store: StateStore,
    bus: EventBus,
    running: Arc<tokio::sync::RwLock<Option<Running>>>,
    generation: Arc<AtomicU64>,
    login: Arc<RwLock<LoginStatus>>,
    connection: APSConnection,
    config: Arc<dyn OSConfig>,
    identity: IDSNGMIdentity,
    anisette: ArcAnisetteClient<DefaultAnisetteProvider>,
    gsa_user: String,
    gsa_pass: Vec<u8>,
    chats: Chats,
}

/// Re-attempt the Apple ID login until it succeeds (or the process exits).
///
/// Deliberately slow: Apple's -80045 block is ecosystem-wide and expected to
/// lift on its own, so this is one attempt at a time with an exponential
/// backoff from `base` up to LOGIN_RETRY_MAX_SECS (30 -> 60 -> 60 ... minutes
/// by default). Exactly one log line is emitted per attempt, and the task stops
/// as soon as an IMClient exists.
async fn retry_login_loop(state: LoginState, base: Duration) {
    let max = Duration::from_secs(LOGIN_RETRY_MAX_SECS.max(base.as_secs()));
    let mut attempt: u32 = 0;
    loop {
        attempt = attempt.saturating_add(1);
        tokio::time::sleep(login_retry_delay(base, max, attempt)).await;

        if is_registered(&state.running).await {
            tracing::info!("Apple ID session is live; stopping the login retry task");
            return;
        }

        // Same panic isolation as the boot attempt.
        let result = match tokio::spawn(complete_apple_id(state.clone())).await {
            Ok(result) => result,
            Err(join_error) => Err(anyhow!("Apple ID login task panicked: {join_error}")),
        };
        match result {
            Ok(()) => {
                tracing::info!(
                    "Apple ID login succeeded on retry attempt {attempt}; stopping the retry task"
                );
                return;
            }
            Err(e) => {
                let next = login_retry_delay(base, max, attempt.saturating_add(1));
                tracing::warn!(
                    "Apple ID login retry attempt {attempt} failed; next attempt in {}s: {e:#}",
                    next.as_secs()
                );
            }
        }
    }
}

/// Exponential backoff bounded by `max`: base, 2*base, 4*base, ... capped.
/// `attempt` is 1-based.
fn login_retry_delay(base: Duration, max: Duration, attempt: u32) -> Duration {
    let shift = attempt.saturating_sub(1).min(16);
    let secs = base.as_secs().saturating_mul(1u64 << shift);
    Duration::from_secs(secs.min(max.as_secs()).max(base.as_secs()))
}

/// Ensure one IDS user (Apple ID, else the saved users) is registered with
/// Apple, then install the IMClient.
async fn complete_apple_id(state: LoginState) -> Result<()> {
    let mut users = state
        .running
        .read()
        .await
        .as_ref()
        .map(|running| running.users.clone())
        .unwrap_or_default();

    if users.is_empty() {
        // Record the attempt before it starts, so /im/status always carries the
        // time Apple was last contacted even if the process dies mid-attempt.
        set_login_attempt(&state.login, None);
        match login_apple_id(
            &state.data_dir,
            &state.connection,
            state.config.as_ref(),
            state.anisette.clone(),
            state.login.clone(),
            state.gsa_user.clone(),
            state.gsa_pass.clone(),
        )
        .await
        {
            Ok(user) => users = vec![user],
            Err(e) => {
                let error = format!("Apple ID login failed: {e:#}");
                set_login_attempt(&state.login, Some(error.clone()));
                return Err(anyhow!(error));
            }
        }
    } else {
        tracing::info!(
            users = users.len(),
            "reusing saved IDS state (no Apple ID login needed)"
        );
    }

    let needs_registration = users
        .first()
        .map(|user| user.registration.is_empty())
        .unwrap_or(true);
    if needs_registration {
        tracing::info!("registering IDS users with Apple");
        if let Err(e) = register(
            state.config.as_ref(),
            &*state.connection.state.read().await,
            id_services!(),
            &mut users,
            &state.identity,
        )
        .await
        {
            let error = format!("IDS registration failed: {e}");
            set_login_attempt(&state.login, Some(error.clone()));
            return Err(anyhow!(error));
        }
    }

    // Full success: Apple ID authenticated and every IDS user registered.
    set_login_attempt(&state.login, None);

    install_registered(
        &state.store,
        &state.bus,
        &state.running,
        &state.generation,
        state.connection.clone(),
        state.config.clone(),
        users,
        state.identity.clone(),
        state.cache_path.clone(),
        state.chats.clone(),
    )
    .await;
    Ok(())
}

/// Record the outcome of a login attempt for GET /im/status: the attempt time
/// always advances, and `error` is stored (or cleared on success).
fn set_login_attempt(login: &RwLock<LoginStatus>, error: Option<String>) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    if let Ok(mut status) = login.write() {
        status.last_attempt = Some(now);
        status.error = error;
    }
}

/// Apple ID login -> IDS delegate -> mailto:<apple id> IDSUser.
/// What the GSA sign-in reports as its client device.
///
/// The anisette machine ID is built by Apple's Android ADI library, so the
/// client must claim Android or GrandSlam answers -80045 at the SRP init
/// POST (2026-10-12/13, ecosystem-wide). Keep the app as akd: the September
/// 2026 GSA edge block still answers HTTP 503 to com.apple.dt.Xcode.
const GSA_AKD_CLIENT_INFO_ANDROID: &str =
    "<Pixel 7> <Android;14;UQ1A.240205.002> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";

async fn login_apple_id(
    data_dir: &Path,
    connection: &APSConnection,
    config: &dyn OSConfig,
    anisette: ArcAnisetteClient<DefaultAnisetteProvider>,
    login: Arc<RwLock<LoginStatus>>,
    user: String,
    pass_sha256: Vec<u8>,
) -> Result<IDSUser> {
    let appleid = move || (user.clone(), pass_sha256.clone());
    let two_factor = two_factor_closure(data_dir.to_path_buf(), login);

    // Pre-flight the anisette provider so an unreachable/blocked backend is
    // reported as its own error instead of hiding inside a generic GSA failure
    // (or, before the omnisette panic fix, aborting the login task). Only
    // non-secret shapes are logged: header count and the lengths of the two
    // attestation values. AnisetteClient caches for 60s, so the login below
    // reuses these very headers rather than fetching a second time.
    match anisette.lock().await.get_headers().await {
        // debug, not info: the retry loop is meant to emit exactly one line per
        // attempt, and this pre-flight runs on every attempt.
        Ok(headers) => tracing::debug!(
            "anisette headers OK: {} headers (X-Apple-I-MD {} chars, X-Apple-I-MD-M {} chars)",
            headers.len(),
            headers.get("X-Apple-I-MD").map(String::len).unwrap_or(0),
            headers.get("X-Apple-I-MD-M").map(String::len).unwrap_or(0),
        ),
        Err(e) => tracing::error!(
            "anisette backend failed - the Apple ID login will fail for that reason: {e}"
        ),
    }

    let mut gsa_config = config.get_gsa_config(&*connection.state.read().await, false);
    // The anisette machine ID comes from Apple's *Android* ADI library (that is
    // what the public anisette servers run), and since 2026-10-12 GrandSlam
    // refuses a sign-in whose client OS does not match the machine ID it is
    // sent with: ec=-80045, 'This action cannot be completed at this time.',
    // returned at the SRP init POST for every account, before any credential
    // is checked. Reporting the GSA client as an Android phone clears it
    // (Mac/Windows/iPhone client-info => -80045, Android => the account check).
    // See Dadoum/anisette-v3-server#61, xtool-org/xtool#337, FrizzleM/
    // SideInstaller@d9c2831 (all 2026-10-13).
    //
    // Only mme_client_info_akd changes. mme_client_info keeps the Mac
    // identity, and with it the URL-bag lookup and the anisette provisioning
    // request (asked as Android the lookup answers with a different, short
    // URL bag), plus the 2FA headers, which GrandSlam rejects as Android.
    gsa_config.mme_client_info_akd = GSA_AKD_CLIENT_INFO_ANDROID.to_string();

    let account = AppleAccount::login(
        appleid,
        two_factor,
        gsa_config,
        anisette,
    )
    .await
    .map_err(|e| anyhow!("AppleAccount::login: {e}"))?;

    let account = rustpush::DebugMutex::new(account);
    account
        .lock()
        .await
        .update_postdata("Apple Device", None, &["icloud", "imessage", "facetime"])
        .await
        .map_err(|e| anyhow!("update_postdata failed: {e}"))?;

    let delegates = {
        let guard = account.lock().await;
        login_apple_delegates(
            &guard,
            None,
            config,
            &[LoginDelegate::IDS, LoginDelegate::MobileMe],
        )
        .await
        .map_err(|e| anyhow!("login_apple_delegates failed: {e}"))?
    };
    let ids_delegate = delegates
        .ids
        .ok_or_else(|| anyhow!("Apple returned no IDS delegate for this account"))?;

    authenticate_apple(ids_delegate, config)
        .await
        .map_err(|e| anyhow!("authenticate_apple failed: {e}"))
}

/// Save the APS state (APNs keypair + token) that this boot's connection
/// produced, however the login goes.
///
/// The APNs token is derived from the keypair in APSState: aps.rs do_connect
/// only calls activate() when state.keypair is None, and the certificate it
/// signs the Connect with decides the token Apple hands back. So an unsaved
/// state means a new keypair and a new token on every restart. That matters
/// because a REG-RESP signature is computed over the token carried in its
/// REG-REQ: replaying a captured signature against a different boot's token is
/// what produced Apple's cert-less
/// "Plist parsing error: missing field cert" instead of the expected 6004.
async fn persist_aps_state(store: &StateStore, connection: &APSConnection) {
    // Clone out of the async lock before doing blocking file IO.
    let state = connection.state.read().await.clone();
    if state.keypair.is_none() && state.token.is_none() {
        // APS has not produced anything yet; spawn_apns_token_watch retries.
        return;
    }
    persist_push_state(store, Some(state));
}

/// Merge a push state into state.plist.backend_state, leaving the IDS users and
/// the NGM identity stored alongside it untouched.
/// Read-modify-write the opaque backend blob: deserialise it, let `update`
/// change the fields it owns, serialise it back. Every writer (APS push state,
/// IDS users, the FaceTime state, the chat map) goes through this so none of
/// them clobbers another's fields.
///
/// Returns true when the blob was written.
fn update_saved(store: &StateStore, update: impl FnOnce(&mut SavedBackend)) -> bool {
    let mut serialised = false;
    let result = store.update_backend_state(|blob| {
        let mut saved: SavedBackend = blob
            .as_ref()
            .and_then(|data| plist::from_bytes(data.as_ref()).ok())
            .unwrap_or_default();
        update(&mut saved);
        let mut buf = Vec::new();
        match plist::to_writer_xml(&mut buf, &saved) {
            Ok(()) => {
                serialised = true;
                Some(Data::new(buf))
            }
            Err(e) => {
                tracing::error!("failed to serialise backend state: {e}");
                // Keep whatever was there rather than dropping the blob.
                blob
            }
        }
    });
    if let Err(e) = result {
        tracing::error!("failed to persist backend state: {e:#}");
    }
    serialised
}

fn persist_push_state(store: &StateStore, push: Option<APSState>) {
    if update_saved(store, |saved| saved.push = push) {
        tracing::info!(
            "APS state persisted (APNs keypair + token) so the token survives a restart"
        );
    }
}

/// Persist the FaceTime link/session state (rustpush FTState) after it changes.
fn persist_facetime(store: &StateStore, state: &rustpush::facetime::FTState) {
    let mut buf = Vec::new();
    match plist::to_writer_xml(&mut buf, state) {
        Ok(()) => {
            update_saved(store, |saved| saved.facetime = Some(Data::new(buf)));
        }
        Err(e) => tracing::error!("failed to serialise FaceTime state: {e}"),
    }
}

/// Persist the conversation map (group replies depend on it).
fn persist_chats(store: &StateStore, chats: &HashMap<String, SavedChat>) {
    let snapshot = chats.clone();
    update_saved(store, |saved| saved.chats = snapshot);
}

/// Persist the live APNs token, if APS has one.
async fn publish_apns_token(store: &StateStore, connection: &APSConnection) {
    let token = connection.state.read().await.token;
    if let Some(token) = token {
        store.set_apns_token(Some(hexutil::encode_hex(&token)));
        tracing::info!("APNs token available ({} bytes)", token.len());
    } else {
        tracing::warn!("APS has no push token yet; /apns-token will 503 until it does");
    }
}

/// Watch for the token appearing after a reconnect (the socket is re-established
/// in the background by rustpush's ResourceManager).
fn spawn_apns_token_watch(store: StateStore, connection: APSConnection) {
    tokio::spawn(async move {
        for _ in 0..120 {
            // Clone the state out of the lock before touching the disk: the
            // keypair is what makes the token stable, so persist both together.
            let state = connection.state.read().await.clone();
            if let Some(token) = state.token {
                store.set_apns_token(Some(hexutil::encode_hex(&token)));
                tracing::info!("APNs token available ({} bytes)", token.len());
                persist_push_state(&store, Some(state));
                return;
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
        tracing::warn!("APS still has no push token after 10 minutes");
    });
}

fn login_retry_secs() -> u64 {
    std::env::var("IMESSAGE_LOGIN_RETRY_SECS")
        .ok()
        .and_then(|raw| raw.trim().parse::<u64>().ok())
        .unwrap_or(DEFAULT_LOGIN_RETRY_SECS)
}

fn init_software_keystore(data_dir: &std::path::Path) -> Result<()> {
    use keystore::software::{NoEncryptor, SoftwareKeystore, SoftwareKeystoreState};
    use std::sync::RwLock;

    let path = data_dir.join("keystore.plist");
    let state: SoftwareKeystoreState = plist::from_file(&path).unwrap_or_default();
    let update_path = path.clone();
    keystore::init_keystore(SoftwareKeystore {
        state: RwLock::new(state),
        update_state: Box::new(move |state| {
            if let Err(e) = plist::to_file_xml(&update_path, state) {
                tracing::error!("failed to persist keystore: {e}");
            }
        }),
        encryptor: NoEncryptor,
    });
    Ok(())
}

/// Clears `two_factor_pending` however the wait ends, including a panic.
struct TwoFactorPendingGuard(Arc<RwLock<LoginStatus>>);

impl Drop for TwoFactorPendingGuard {
    fn drop(&mut self) {
        if let Ok(mut status) = self.0.write() {
            status.two_factor_pending = false;
        }
    }
}

/// The 6-digit 2FA code, with no human in the loop.
///
/// Precedence: IMESSAGE_2FA_CODE, then a code an operator left in
/// <data_dir>/2fa.txt, then (the normal path) the SMS that Apple texts to the
/// trusted number, read straight off the handset over adb - see sms2fa.rs.
///
/// rustpush's login loop asks GSA to text the code (icloud-auth client.rs:
/// LoginState::NeedsSMS2FA/NeedsDevice2FA -> send_sms_2fa_to_devices ->
/// verify_sms_2fa -> this closure) rather than relying on a trusted-device
/// prompt; that is what this account needs, because its only trusted device
/// (an iPhone X) will not power on.
///
/// Blocks (up to 10 minutes): rustpush's login API takes a synchronous
/// closure, so the wait cannot be made async. It is wrapped in
/// `tokio::task::block_in_place`, which hands the worker's core (and with it
/// the runtime's I/O driver) to a fresh thread before blocking, so the HTTP
/// API keeps answering on `/health`, `/im/status` and `/apns-token`
/// throughout the wait. Without it a blocked worker stalls the whole
/// multi-threaded runtime: the listener accepts nothing (the accept queue
/// grows) and every endpoint hangs for the entire 600 s.
fn two_factor_closure(
    data_dir: PathBuf,
    login: Arc<RwLock<LoginStatus>>,
) -> impl Fn() -> String + Send + Sync + 'static {
    // Snapshot the handset's SMS mirror *now*, i.e. before the login asks Apple
    // for anything. The login loop requests the SMS before it invokes this
    // closure, so a snapshot taken inside the closure would already contain the
    // new code and it would look stale.
    let watcher = crate::sms2fa::SmsWatcher::from_env();
    let serial = watcher.serial().to_string();
    let path = data_dir.join("2fa.txt");
    // Start each attempt clean: a code left in 2fa.txt by a previous attempt is
    // long expired and must not be handed to Apple again.
    if let Ok(raw) = std::fs::read_to_string(&path) {
        if is_six_digits(raw.trim()) {
            tracing::info!(
                "discarding the stale 2FA code left in {} by a previous attempt",
                path.display()
            );
            let _ = std::fs::remove_file(&path);
        }
    }

    move || {
        if let Ok(code) = std::env::var("IMESSAGE_2FA_CODE") {
            let code = code.trim().to_string();
            if !code.is_empty() {
                return code;
            }
        }

        if let Ok(raw) = std::fs::read_to_string(&path) {
            let code = raw.trim().to_string();
            if is_six_digits(&code) {
                let _ = std::fs::remove_file(&path);
                tracing::info!("using the 2FA code an operator wrote to {}", path.display());
                return code;
            }
        }

        // The SMS has already been requested by icloud-auth (its
        // request_sms_2fa -> verify_sms_2fa path invokes this closure), so this
        // line is the confirmation that Apple was asked. Name both ways the
        // operator can hand the code over.
        tracing::info!(
            "asked Apple to text a code to {}; write it to {} (or set \
             IMESSAGE_2FA_CODE) - the wait will pick up either one",
            two_factor_target_label(),
            path.display()
        );
        tracing::warn!(
            "TWO-FACTOR AUTHENTICATION REQUIRED - waiting up to {}s, polling {} and \
             IMESSAGE_2FA_CODE and the handset over adb (serial {serial}) every 2s; a code \
             written after this line is used",
            TWO_FACTOR_TIMEOUT_SECS,
            path.display()
        );

        // Publish the wait on GET /im/status, and block WITHOUT stalling the
        // runtime: block_in_place moves this worker's core to another thread
        // first, so the HTTP server keeps serving while we poll.
        if let Ok(mut status) = login.write() {
            status.two_factor_pending = true;
        }
        let _pending = TwoFactorPendingGuard(login.clone());
        let found = tokio::task::block_in_place(|| {
            wait_for_2fa(&path, &watcher, TWO_FACTOR_TIMEOUT_SECS)
        });

        match found {
            Some(code) => code,
            None => {
                tracing::error!(
                    "timed out after {}s waiting for a 2FA code; none was written to {} and \
                     no NEW inbound SMS with a 6-digit code appeared on adb serial {serial}",
                    TWO_FACTOR_TIMEOUT_SECS,
                    path.display()
                );
                String::new()
            }
        }
    }
}

/// Poll for the 2FA code until the deadline.
///
/// Re-reads `IMESSAGE_2FA_CODE` and `path` on every pass, so a code the
/// operator writes *after* the wait begins is picked up - the previous version
/// read the file once and then blocked for the whole timeout, which defeated
/// the operator-supplied path. Also polls the handset over adb for Apple's SMS.
/// Returns None only on timeout.
fn wait_for_2fa(
    path: &Path,
    watcher: &crate::sms2fa::SmsWatcher,
    timeout_secs: u64,
) -> Option<String> {
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);
    let mut announced_adb = false;
    loop {
        if let Some(code) = operator_code(path) {
            return Some(code);
        }
        if !announced_adb {
            tracing::info!(
                "no operator code yet; also polling the handset over adb (serial {})",
                watcher.serial()
            );
            announced_adb = true;
        }
        if let Some(found) = watcher.poll_once() {
            tracing::info!(
                "2FA code read from the handset over adb - from={} device_time={:?} \
                 (read off the device, NOT typed); writing {} for the record",
                found.from,
                found.timestamp_ms,
                path.display()
            );
            if let Err(e) = std::fs::write(path, format!("{}\n", found.code)) {
                tracing::warn!("could not write {}: {e}", path.display());
            }
            return Some(found.code);
        }
        if Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(Duration::from_secs(2));
    }
}

/// A code the operator supplied: `IMESSAGE_2FA_CODE` first, then the file at
/// `path` (removed once consumed). None if neither currently has one.
fn operator_code(path: &Path) -> Option<String> {
    if let Ok(code) = std::env::var("IMESSAGE_2FA_CODE") {
        let code = code.trim().to_string();
        if !code.is_empty() {
            tracing::info!("using the 2FA code from IMESSAGE_2FA_CODE");
            return Some(code);
        }
    }
    if let Ok(raw) = std::fs::read_to_string(path) {
        let code = raw.trim().to_string();
        if is_six_digits(&code) {
            let _ = std::fs::remove_file(path);
            tracing::info!("using the 2FA code an operator wrote to {}", path.display());
            return Some(code);
        }
    }
    None
}

/// The phone the SMS targets, for operator logs: `...8286` when
/// `IMESSAGE_2FA_PHONE` is set, else a neutral label. Never the full number.
fn two_factor_target_label() -> String {
    std::env::var("IMESSAGE_2FA_PHONE")
        .ok()
        .map(|v| v.chars().filter(|c| c.is_ascii_digit()).collect::<String>())
        .filter(|v| !v.is_empty())
        .map(|digits| format!("...{}", &digits[digits.len().saturating_sub(4)..]))
        .unwrap_or_else(|| "the account's trusted phone".to_string())
}

fn is_six_digits(code: &str) -> bool {
    code.len() == 6 && code.chars().all(|c| c.is_ascii_digit())
}

/// reply_to accepts r:<part>:<guid> (the wire form) or a bare message GUID.
fn split_reply(reply_to: Option<String>) -> (Option<String>, Option<String>) {
    let Some(value) = reply_to else {
        return (None, None);
    };
    let value = value.trim();
    if value.is_empty() {
        return (None, None);
    }
    let parts: Vec<&str> = value.splitn(3, ':').collect();
    if parts.len() == 3 && (parts[0] == "r" || parts[0] == "p") {
        (Some(parts[2].to_string()), Some(parts[1].to_string()))
    } else {
        // MessageInst::to_raw unwraps reply_part whenever reply_guid is set.
        (Some(value.to_string()), Some("0".to_string()))
    }
}

/// Handles Apple advertised for the Madrid (iMessage) service.
fn handles_from(users: &[IDSUser]) -> Vec<String> {
    let mut out = Vec::new();
    for user in users {
        if let Some(registration) = user.registration.get("com.apple.madrid") {
            for handle in &registration.handles {
                if !out.contains(handle) {
                    out.push(handle.clone());
                }
            }
        }
    }
    out
}

/// The keys_updated callback: rustpush calls it (sync) whenever the identity
/// cache changes, so persist the new user set. Blocking file IO on purpose -
/// this is a small plist, and the alternative is losing key material.
fn make_keys_updated(
    store: StateStore,
    identity: IDSNGMIdentity,
    push: Option<APSState>,
) -> Box<dyn FnMut(Vec<IDSUser>) + Send + Sync> {
    Box::new(move |users: Vec<IDSUser>| {
        let handles = handles_from(&users);
        persist_backend(&store, push.clone(), &users, &identity);
        store.set_handles(handles);
    })
}

fn persist_backend(
    store: &StateStore,
    push: Option<APSState>,
    users: &[IDSUser],
    identity: &IDSNGMIdentity,
) {
    // Same read-modify-write path as persist_push_state, so a push state or a
    // FaceTime state saved concurrently is not lost (and vice versa).
    update_saved(store, |saved| {
        saved.push = push;
        saved.users = users.to_vec();
        saved.identity = Some(identity.clone());
    });
}

/// The `ConversationData` a group reply is sent with: the recorded member
/// list, and the recorded iMessage group id as `sender_guid`. Those two fields
/// are what rustpush puts on the wire (`to_raw` -> `RawIMessage.p` / `.gid`),
/// so reusing them is what makes the reply land in the existing thread.
fn group_conversation(chat: &SavedChat, sender_guid: String) -> ConversationData {
    ConversationData {
        participants: chat.participants.clone(),
        cv_name: None,
        sender_guid: Some(sender_guid),
        after_guid: None,
    }
}

/// Record the conversation an inbound message belongs to, so `/im/send` can
/// reply into it by `chat_guid`.
///
/// The key is the iMessage group id (the wire `gid`), which is exactly what
/// `message_event` publishes as `chat_guid` when the message carries one. That
/// id plus the member list is all rustpush needs to send into the same thread:
/// see `MessageInst::prepare_send` / `to_raw`, which put `conversation
/// .participants` and `conversation.sender_guid` straight on the wire.
fn remember_chat(chats: &Chats, store: &StateStore, inst: &MessageInst) {
    let Some(conversation) = inst.conversation.as_ref() else {
        return;
    };
    let Some(sender_guid) = conversation.sender_guid.clone() else {
        // No group id on the wire: nothing a reply could be addressed to.
        return;
    };

    let mut participants: Vec<String> = conversation
        .participants
        .iter()
        .map(|participant| normalize_handle(participant))
        .collect();
    participants.sort();
    participants.dedup();
    if participants.is_empty() {
        return;
    }

    let updated_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0);
    let chat = SavedChat {
        participants,
        sender_guid: Some(sender_guid.clone()),
        updated_ms,
    };

    let snapshot = {
        let Ok(mut map) = chats.write() else {
            return;
        };
        // Only rewrite state.plist when the conversation is new or its member
        // list changed (a group add/remove); ordinary messages are no-ops.
        let changed = map
            .get(&sender_guid)
            .map(|existing| existing.participants != chat.participants)
            .unwrap_or(true);
        map.insert(sender_guid.clone(), chat);
        if map.len() > MAX_CHATS {
            if let Some(oldest) = map
                .iter()
                .min_by_key(|(_, chat)| chat.updated_ms)
                .map(|(guid, _)| guid.clone())
            {
                map.remove(&oldest);
            }
        }
        if changed {
            Some(map.clone())
        } else {
            None
        }
    };

    if let Some(snapshot) = snapshot {
        persist_chats(store, &snapshot);
        tracing::debug!(chat_guid = %sender_guid, "recorded conversation for group replies");
    }
}

/// Turn an inbound MessageInst into the frozen SSE shape. Non-text messages
/// (receipts, typing, profile updates, errors) are not surfaced yet.
fn message_event(inst: &MessageInst) -> Option<MessageEvent> {
    let text = match &inst.message {
        Message::Message(normal) => normal.parts.raw_text(),
        _ => return None,
    };
    if text.is_empty() {
        return None;
    }

    let from = inst.sender.as_deref().map(strip_handle).unwrap_or_default();
    let participants: Vec<String> = inst
        .conversation
        .as_ref()
        .map(|c| c.participants.iter().map(|p| strip_handle(p)).collect())
        .unwrap_or_default();
    let to: Vec<String> = participants
        .iter()
        .filter(|p| **p != from)
        .cloned()
        .collect();
    let is_group = inst
        .conversation
        .as_ref()
        .map(|c| c.is_group())
        .unwrap_or(false)
        || to.len() > 1;
    let chat_guid = inst
        .conversation
        .as_ref()
        .and_then(|c| c.sender_guid.clone())
        .unwrap_or_else(|| chat_guid_for(&from, &to));

    Some(MessageEvent {
        guid: inst.id.clone(),
        chat_guid,
        from,
        to,
        text,
        timestamp: inst.sent_timestamp as i64,
        is_group,
        sender_name: None,
    })
}

/// The push loop: hand every APS message to the IMClient and forward inbound
/// text messages to the SSE bus. Exits when the client is rebuilt.
async fn receive_loop(
    connection: APSConnection,
    client: Arc<IMClient>,
    bus: EventBus,
    generation: Arc<AtomicU64>,
    my_generation: u64,
    chats: Chats,
    store: StateStore,
) {
    let mut subscription = connection.messages_cont.subscribe();
    loop {
        if generation.load(Ordering::SeqCst) != my_generation {
            tracing::debug!("push loop for generation {my_generation} exiting (client rebuilt)");
            return;
        }

        match subscription.recv().await {
            Ok(message) => match client.handle(message).await {
                Ok(Some(inst)) => {
                    if let Some(context) = inst.certified_context.clone() {
                        if let Err(e) = client
                            .identity
                            .certify_delivery("com.apple.madrid", &context, false)
                            .await
                        {
                            tracing::warn!("certify_delivery failed: {e}");
                        }
                    }
                    // Learn the conversation before publishing it, so a
                    // reply can address this chat_guid immediately.
                    remember_chat(&chats, &store, &inst);
                    if let Some(event) = message_event(&inst) {
                        tracing::info!(guid = %event.guid, from = %event.from, "inbound message");
                        bus.message(event);
                    }
                }
                Ok(None) => {}
                Err(e) => tracing::warn!("failed to handle a push message: {e}"),
            },
            Err(tokio::sync::broadcast::error::RecvError::Lagged(count)) => {
                tracing::warn!("dropped {count} push messages (subscriber lagged)");
            }
            Err(tokio::sync::broadcast::error::RecvError::Closed) => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reply_to_wire_form() {
        assert_eq!(split_reply(None), (None, None));
        assert_eq!(
            split_reply(Some("r:0:ABCD".into())),
            (Some("ABCD".into()), Some("0".into()))
        );
        assert_eq!(
            split_reply(Some("ABCD".into())),
            (Some("ABCD".into()), Some("0".into()))
        );
    }

    #[test]
    fn login_retry_backoff_is_bounded() {
        let base = Duration::from_secs(1800);
        let max = Duration::from_secs(LOGIN_RETRY_MAX_SECS);
        // 30 min, then 60 min, then stay at the cap.
        assert_eq!(login_retry_delay(base, max, 1).as_secs(), 1800);
        assert_eq!(login_retry_delay(base, max, 2).as_secs(), 3600);
        assert_eq!(login_retry_delay(base, max, 9).as_secs(), 3600);
        // A smaller override backs off too, and still never exceeds the cap.
        let fast = Duration::from_secs(60);
        assert_eq!(login_retry_delay(fast, max, 1).as_secs(), 60);
        assert_eq!(login_retry_delay(fast, max, 7).as_secs(), 3600);
        assert_eq!(login_retry_delay(fast, max, 20).as_secs(), 3600);
    }

    fn temp_state(name: &str) -> (PathBuf, StateStore) {
        let path = std::env::temp_dir().join(format!(
            "imessage-relay-test-{}-{name}.plist",
            uuid::Uuid::new_v4()
        ));
        let store = StateStore::load(path.clone(), "me@example.com".into(), EventBus::new())
            .expect("temp state store");
        (path, store)
    }

    fn group_inst(sender_guid: &str) -> MessageInst {
        MessageInst::new(
            ConversationData {
                participants: vec![
                    "tel:+15551234567".into(),
                    "mailto:friend@example.com".into(),
                    "tel:+15551230000".into(),
                ],
                cv_name: None,
                sender_guid: Some(sender_guid.to_string()),
                after_guid: None,
            },
            "mailto:friend@example.com",
            Message::Message(NormalMessage::new("hi".into(), MessageType::IMessage)),
        )
    }

    /// An inbound group message must be recorded under the same chat_guid that
    /// /im/events publishes (the wire `gid`), and it must reach state.plist so a
    /// reply still works after a restart.
    #[test]
    fn remember_chat_records_group_and_persists() {
        let (path, store) = temp_state("chats");
        let chats: Chats = Arc::new(RwLock::new(HashMap::new()));

        remember_chat(&chats, &store, &group_inst("GID-GROUP-1"));

        {
            let map = chats.read().unwrap();
            let chat = map
                .get("GID-GROUP-1")
                .expect("recorded under the group id");
            assert_eq!(
                chat.participants,
                vec![
                    "mailto:friend@example.com".to_string(),
                    "tel:+15551230000".to_string(),
                    "tel:+15551234567".to_string(),
                ]
            );
            assert_eq!(chat.sender_guid.as_deref(), Some("GID-GROUP-1"));
        }

        let blob = store.backend_state().expect("backend blob written");
        let saved: SavedBackend = plist::from_bytes(blob.as_ref()).unwrap();
        assert!(saved.chats.contains_key("GID-GROUP-1"));
        let _ = std::fs::remove_file(path);
    }

    /// A group reply goes out with the recorded member list *and* the recorded
    /// group id; losing either would create a new thread or drop recipients.
    #[test]
    fn group_conversation_carries_members_and_group_id() {
        let chat = SavedChat {
            participants: vec!["tel:+1".into(), "tel:+2".into(), "mailto:a@b".into()],
            sender_guid: Some("GID".into()),
            updated_ms: 0,
        };
        let conversation = group_conversation(&chat, "GID".into());
        assert_eq!(conversation.participants, chat.participants);
        assert_eq!(conversation.sender_guid.as_deref(), Some("GID"));
        assert!(conversation.is_group());
    }

    /// The chat map is what makes a chat_guid resolvable; an unknown one must
    /// stay unknown rather than silently becoming a 1:1 send.
    #[test]
    fn unknown_chat_guid_is_not_resolved() {
        let chats: Chats = Arc::new(RwLock::new(HashMap::new()));
        assert!(chats.read().unwrap().get("iMessage;+;nope").is_none());
    }
}
