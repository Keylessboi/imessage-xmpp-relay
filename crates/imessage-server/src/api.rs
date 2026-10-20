//! The frozen local HTTP API.
//!
//!
//! GET  /health            -> {"ok":true,"registered":bool}
//! GET  /im/status         -> {"registered":bool,"handles":[str],"apple_id":str,
//!                             "login_error":str|null,"last_login_attempt":int|null}
//! POST /im/send           <- {"to":str,"text":str,"reply_to":str|null}
//!                            or {"chat_guid":str,"text":str,"reply_to":str|null}
//!                         -> {"ok":true,"guid":str} | non-2xx {"error":str}
//! GET  /im/events         -> text/event-stream of the JSON in events.rs
//! POST /im/read|typing|react|edit|unsend   (additive; see `ActionBody`)
//!                         <- {"to"|"chat_guid":str, ...} -> {"ok":true,"guid":str}
//! POST /im/register-phone <- {"number":str,"signature":<hex>} -> {"ok":true}
//! POST /im/facetime-link  <- {"handle":str|null} -> {"ok":true,"url":str}
//! GET  /apns-token        -> {"token":"<hex>"} | 503 {"error":str}
//! (group endpoints: additive, see groups.rs)
//!
//!
//! Do not change these shapes; another service is written against them.

use std::convert::Infallible;
use std::sync::Arc;

use axum::extract::rejection::JsonRejection;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::sse::{Event as SseEvent, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::StreamExt;

use crate::backend::{Action, Backend, BackendError};
use crate::events::{Event, EventBus};
use crate::hexutil;
use crate::state::StateStore;

#[derive(Clone)]
pub struct AppState {
    pub store: StateStore,
    pub backend: Arc<dyn Backend>,
    pub bus: EventBus,
}

impl AppState {
    pub fn new(store: StateStore, backend: Arc<dyn Backend>, bus: EventBus) -> Self {
        Self {
            store,
            backend,
            bus,
        }
    }
}

/// `(status, {"error": "..."})` - the only error shape the API emits.
#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub message: String,
}

impl ApiError {
    pub fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
    }
}

impl From<BackendError> for ApiError {
    fn from(e: BackendError) -> Self {
        Self::new(e.status, e.message)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "error": self.message }))).into_response()
    }
}

fn json_event(event: &Event) -> SseEvent {
    let data = serde_json::to_string(event).unwrap_or_else(|_| "{}".to_string());
    SseEvent::default().data(data)
}

// ---------------------------------------------------------------- handlers --

async fn health(State(app): State<Arc<AppState>>) -> Json<Value> {
    Json(json!({ "ok": true, "registered": app.store.registered() }))
}

async fn im_status(State(app): State<Arc<AppState>>) -> Json<Value> {
    let state = app.store.snapshot();
    // Additive only: every pre-existing field and its type is untouched, and
    // the two new fields are always present (null when unknown). Other services
    // are written against this shape.
    let login = app.backend.login_status();
    Json(json!({
        "registered": state.registered,
        "handles": state.handles,
        "apple_id": state.apple_id,
        "login_error": login.error,
        "last_login_attempt": login.last_attempt,
        "two_factor_pending": login.two_factor_pending,
    }))
}

#[derive(Deserialize)]
struct SendBody {
    /// 1:1 recipient. Optional now that chat_guid exists: a group send names
    /// the conversation instead of a handle. When neither is supplied the
    /// pre-existing "'to' must not be empty" 400 is returned, so a caller that
    /// only ever passes `to` sees no change.
    #[serde(default)]
    to: String,
    text: String,
    #[serde(default)]
    reply_to: Option<String>,
    /// Group conversation to reply into: the chat_guid from an inbound
    /// /im/events message. Taken verbatim; the backend owns the rest of the
    /// group state (member list and group id) it recorded for that chat.
    #[serde(default)]
    chat_guid: Option<String>,
}

async fn im_send(
    State(app): State<Arc<AppState>>,
    body: Result<Json<SendBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let Json(body) =
        body.map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, format!("invalid body: {e}")))?;

    let chat_guid = body
        .chat_guid
        .as_deref()
        .map(str::trim)
        .filter(|guid| !guid.is_empty());

    if chat_guid.is_none() && body.to.trim().is_empty() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "'to' must not be empty",
        ));
    }
    if body.text.trim().is_empty() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "'text' must not be empty",
        ));
    }
    if !app.store.registered() {
        return Err(ApiError::from(BackendError::not_registered()));
    }

    let guid = match chat_guid {
        // Group reply: the backend resolves the recorded participant list and
        // group id for this chat_guid.
        Some(chat_guid) => {
            app.backend
                .send_group(chat_guid, &body.text, body.reply_to)
                .await?
        }
        // Exactly the previous 1:1 path, unchanged.
        None => {
            app.backend
                .send_message(body.to.trim(), &body.text, body.reply_to)
                .await?
        }
    };
    Ok(Json(json!({ "ok": true, "guid": guid })))
}

/// One body for the five action endpoints; each endpoint requires its own subset.
///   /im/read   guid
///   /im/typing typing (default true)
///   /im/react  guid, reaction, [emoji], [enable=true], [part], [to_text]
///   /im/edit   guid, text, [part=0]
///   /im/unsend guid, [part=0]
/// Every endpoint also needs `to` or `chat_guid`.
#[derive(Deserialize, Default)]
#[serde(default)]
struct ActionBody {
    to: String,
    chat_guid: Option<String>,
    guid: String,
    part: Option<u64>,
    typing: Option<bool>,
    reaction: String,
    emoji: Option<String>,
    enable: Option<bool>,
    to_text: String,
    text: String,
}

const REACTIONS: [&str; 7] = [
    "heart", "like", "dislike", "laugh", "emphasize", "question", "emoji",
];

/// Validate an action body into a backend `Action` (pure; unit-tested).
fn build_action(kind: &str, body: ActionBody) -> Result<Action, String> {
    let need_guid = |body: &ActionBody| {
        if body.guid.trim().is_empty() {
            Err("'guid' must not be empty".to_string())
        } else {
            Ok(body.guid.trim().to_string())
        }
    };
    match kind {
        "read" => Ok(Action::Read { guid: need_guid(&body)? }),
        "typing" => Ok(Action::Typing { typing: body.typing.unwrap_or(true) }),
        "react" => {
            let guid = need_guid(&body)?;
            let reaction = body.reaction.trim().to_lowercase();
            if !REACTIONS.contains(&reaction.as_str()) {
                return Err(format!("'reaction' must be one of {}", REACTIONS.join("|")));
            }
            let emoji = body.emoji.filter(|e| !e.trim().is_empty());
            if reaction == "emoji" && emoji.is_none() {
                return Err("'emoji' is required when reaction is 'emoji'".to_string());
            }
            Ok(Action::React {
                guid,
                part: body.part,
                reaction,
                emoji,
                enable: body.enable.unwrap_or(true),
                to_text: body.to_text,
            })
        }
        "edit" => {
            let guid = need_guid(&body)?;
            if body.text.trim().is_empty() {
                return Err("'text' must not be empty".to_string());
            }
            Ok(Action::Edit { guid, part: body.part.unwrap_or(0), text: body.text })
        }
        "unsend" => Ok(Action::Unsend { guid: need_guid(&body)?, part: body.part.unwrap_or(0) }),
        other => Err(format!("unknown action `{other}`")),
    }
}

async fn run_action(
    app: Arc<AppState>,
    kind: &str,
    body: Result<Json<ActionBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let Json(body) =
        body.map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, format!("invalid body: {e}")))?;
    let to = body.to.trim().to_string();
    let chat_guid = body
        .chat_guid
        .as_deref()
        .map(str::trim)
        .filter(|g| !g.is_empty())
        .map(str::to_string);
    if chat_guid.is_none() && to.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "'to' must not be empty"));
    }
    let action = build_action(kind, body).map_err(|m| ApiError::new(StatusCode::BAD_REQUEST, m))?;
    if !app.store.registered() {
        return Err(ApiError::from(BackendError::not_registered()));
    }
    let guid = app.backend.send_action(&to, chat_guid.as_deref(), action).await?;
    Ok(Json(json!({ "ok": true, "guid": guid })))
}

async fn im_read(State(app): State<Arc<AppState>>, body: Result<Json<ActionBody>, JsonRejection>) -> Result<Json<Value>, ApiError> {
    run_action(app, "read", body).await
}
async fn im_typing(State(app): State<Arc<AppState>>, body: Result<Json<ActionBody>, JsonRejection>) -> Result<Json<Value>, ApiError> {
    run_action(app, "typing", body).await
}
async fn im_react(State(app): State<Arc<AppState>>, body: Result<Json<ActionBody>, JsonRejection>) -> Result<Json<Value>, ApiError> {
    run_action(app, "react", body).await
}
async fn im_edit(State(app): State<Arc<AppState>>, body: Result<Json<ActionBody>, JsonRejection>) -> Result<Json<Value>, ApiError> {
    run_action(app, "edit", body).await
}
async fn im_unsend(State(app): State<Arc<AppState>>, body: Result<Json<ActionBody>, JsonRejection>) -> Result<Json<Value>, ApiError> {
    run_action(app, "unsend", body).await
}

#[derive(Deserialize)]
struct FaceTimeLinkBody {
    /// Optional context only: a FaceTime link is not tied to one recipient.
    /// If this names one of the server's own handles it is used as the link's
    /// origin; otherwise the server's phone-number handle is.
    #[serde(default)]
    handle: Option<String>,
}

/// POST /im/facetime-link - mint (or reuse) a https://facetime.apple.com/join#...
/// link from the registered IDS identity.
async fn im_facetime_link(
    State(app): State<Arc<AppState>>,
    body: Result<Json<FaceTimeLinkBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    // An empty JSON object is fine; the handle is only context.
    let Json(body) = body
        .map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, format!("invalid body: {e}")))?;

    if !app.store.registered() {
        return Err(ApiError::from(BackendError::not_registered()));
    }

    let url = app.backend.facetime_link(body.handle.as_deref()).await?;
    Ok(Json(json!({ "ok": true, "url": url })))
}

async fn im_events(
    State(app): State<Arc<AppState>>,
) -> Sse<impl tokio_stream::Stream<Item = Result<SseEvent, Infallible>>> {
    // A client that connects after registration still gets its ready frame.
    let initial = if app.store.registered() {
        vec![json_event(&Event::Ready)]
    } else {
        Vec::new()
    };

    let receiver = app.bus.subscribe();
    let stream = tokio_stream::iter(initial.into_iter().map(Ok)).chain(
        BroadcastStream::new(receiver)
            .filter_map(|item| item.ok())
            .map(|event| Ok(json_event(&event))),
    );

    Sse::new(stream).keep_alive(KeepAlive::default())
}

#[derive(Deserialize)]
struct RegisterPhoneBody {
    number: String,
    signature: String,
}

async fn register_phone(
    State(app): State<Arc<AppState>>,
    body: Result<Json<RegisterPhoneBody>, JsonRejection>,
) -> Result<Json<Value>, ApiError> {
    let Json(body) =
        body.map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, format!("invalid body: {e}")))?;

    let number = body.number.trim().to_string();
    if !number.starts_with('+') {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "'number' must be E.164, e.g. +15551234567",
        ));
    }
    if hexutil::decode_hex(&body.signature).is_none() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "'signature' must be the hex signature from REG-RESP",
        ));
    }
    // The real backend can mint a P:<number> IDS user from the APNs token and
    // the carrier signature alone, so do not require a completed Apple ID
    // login here; the backend reports Apple's own error text when it cannot.
    if !app.store.registered() && !app.backend.phone_registration_available() {
        return Err(ApiError::from(BackendError::not_registered()));
    }

    app.backend.register_phone(&number, &body.signature).await?;
    Ok(Json(
        json!({ "ok": true, "handle": format!("tel:{number}") }),
    ))
}

async fn apns_token(State(app): State<Arc<AppState>>) -> Result<Json<Value>, ApiError> {
    let token = app.backend.apns_token().await?;
    Ok(Json(json!({ "token": token })))
}

// ------------------------------------------------------------------ router --

pub fn router(app: Arc<AppState>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/im/status", get(im_status))
        .route("/im/send", post(im_send))
        .route("/im/events", get(im_events))
        .route("/im/read", post(im_read))
        .route("/im/typing", post(im_typing))
        .route("/im/react", post(im_react))
        .route("/im/edit", post(im_edit))
        .route("/im/unsend", post(im_unsend))
        .route("/im/register-phone", post(register_phone))
        .route("/im/facetime-link", post(im_facetime_link))
        .route("/apns-token", get(apns_token))
        .merge(crate::groups::routes())
        .with_state(app)
}

#[cfg(test)]
mod action_tests {
    use super::*;

    fn body(f: impl FnOnce(&mut ActionBody)) -> ActionBody {
        let mut b = ActionBody::default();
        b.guid = "G".into();
        f(&mut b);
        b
    }

    #[test]
    fn read_and_unsend_need_a_guid() {
        assert!(build_action("read", ActionBody::default()).is_err());
        assert_eq!(
            build_action("read", body(|_| {})).unwrap(),
            Action::Read { guid: "G".into() }
        );
        assert_eq!(
            build_action("unsend", body(|b| b.part = Some(2))).unwrap(),
            Action::Unsend { guid: "G".into(), part: 2 }
        );
    }

    #[test]
    fn typing_defaults_to_on() {
        assert_eq!(
            build_action("typing", ActionBody::default()).unwrap(),
            Action::Typing { typing: true }
        );
        assert_eq!(
            build_action("typing", body(|b| b.typing = Some(false))).unwrap(),
            Action::Typing { typing: false }
        );
    }

    #[test]
    fn react_validates_the_reaction_name() {
        assert!(build_action("react", body(|b| b.reaction = "wink".into())).is_err());
        assert!(build_action("react", body(|b| b.reaction = "emoji".into())).is_err());
        let ok = build_action("react", body(|b| b.reaction = "Heart".into())).unwrap();
        assert_eq!(
            ok,
            Action::React {
                guid: "G".into(),
                part: None,
                reaction: "heart".into(),
                emoji: None,
                enable: true,
                to_text: String::new(),
            }
        );
        let e = build_action(
            "react",
            body(|b| {
                b.reaction = "emoji".into();
                b.emoji = Some("🎉".into());
                b.enable = Some(false);
            }),
        )
        .unwrap();
        assert!(matches!(e, Action::React { enable: false, .. }));
    }

    #[test]
    fn edit_needs_text() {
        assert!(build_action("edit", body(|_| {})).is_err());
        assert_eq!(
            build_action("edit", body(|b| b.text = "fixed".into())).unwrap(),
            Action::Edit { guid: "G".into(), part: 0, text: "fixed".into() }
        );
    }
}
