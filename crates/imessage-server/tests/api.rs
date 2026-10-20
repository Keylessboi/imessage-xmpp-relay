//! End-to-end checks of the frozen HTTP surface against the stub backend.
//!
//! These run without any Apple account; they prove the API shapes, the
//! registration gate and the SSE framing.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use imessage_server::backend::{Backend, BackendError, LoginStatus, StubBackend};
use imessage_server::events::{Event, EventBus, MessageEvent};
use imessage_server::state::StateStore;
use imessage_server::{api, api::AppState};
use tower::ServiceExt;

struct Harness {
    app: axum::Router,
    store: StateStore,
    bus: EventBus,
}

fn harness() -> Harness {
    let dir = std::env::temp_dir().join(format!("ims-api-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let bus = EventBus::new();
    let store = StateStore::load(
        dir.join("state.plist"),
        "me@example.com".into(),
        bus.clone(),
    )
    .unwrap();
    let backend: Arc<dyn Backend> = Arc::new(StubBackend::new(store.clone()));
    let app = api::router(Arc::new(AppState::new(store.clone(), backend, bus.clone())));
    Harness { app, store, bus }
}

async fn send(harness: &Harness, req: Request<Body>) -> (StatusCode, serde_json::Value) {
    let response = harness.app.clone().oneshot(req).await.unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let value = if bytes.is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null)
    };
    (status, value)
}

fn get(uri: &str) -> Request<Body> {
    Request::builder().uri(uri).body(Body::empty()).unwrap()
}

fn post_json(uri: &str, body: serde_json::Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

#[tokio::test]
async fn health_and_status_before_registration() {
    let h = harness();

    let (status, body) = send(&h, get("/health")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, serde_json::json!({ "ok": true, "registered": false }));

    let (status, body) = send(&h, get("/im/status")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["registered"], serde_json::json!(false));
    assert_eq!(body["apple_id"], serde_json::json!("me@example.com"));
    assert_eq!(body["handles"], serde_json::json!([]));
}

#[tokio::test]
async fn gated_endpoints_return_503_with_error_shape() {
    let h = harness();

    let (status, body) = send(
        &h,
        post_json(
            "/im/send",
            serde_json::json!({ "to": "+15551234567", "text": "hi", "reply_to": null }),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body, serde_json::json!({ "error": "not registered" }));

    let (status, body) = send(
        &h,
        post_json(
            "/im/register-phone",
            serde_json::json!({ "number": "+15551234567", "signature": "aabbcc" }),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body, serde_json::json!({ "error": "not registered" }));

    let (status, body) = send(&h, get("/apns-token")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(body["error"].is_string());
}

#[tokio::test]
async fn send_validates_the_body() {
    let h = harness();

    let (status, body) = send(
        &h,
        post_json("/im/send", serde_json::json!({ "text": "hi" })),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"].is_string());

    let (status, _) = send(
        &h,
        post_json(
            "/im/send",
            serde_json::json!({ "to": "+1555", "text": "   " }),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, body) = send(
        &h,
        Request::builder()
            .method("POST")
            .uri("/im/send")
            .header("content-type", "application/json")
            .body(Body::from("not json"))
            .unwrap(),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"].is_string());
}

#[tokio::test]
async fn send_succeeds_once_registered() {
    let h = harness();
    h.store
        .set_registered(true, vec!["mailto:me@example.com".into()]);

    // The stub still cannot send, but the answer is no longer the gate's
    // "not registered" - it names the actual limitation.
    let (status, body) = send(
        &h,
        post_json(
            "/im/send",
            serde_json::json!({ "to": "+15551234567", "text": "hi", "reply_to": null }),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_ne!(body["error"], serde_json::json!("not registered"));
    assert!(body["error"].as_str().unwrap().contains("stub backend"));

    let (status, body) = send(&h, get("/health")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["registered"], serde_json::json!(true));

    let (_, body) = send(&h, get("/im/status")).await;
    assert_eq!(
        body["handles"],
        serde_json::json!(["mailto:me@example.com"])
    );
}

#[tokio::test]
async fn group_send_is_routed_from_chat_guid() {
    let h = harness();
    h.store.set_registered(true, vec![]);

    // chat_guid alone is a valid request: `to` is only required for 1:1.
    let (status, body) = send(
        &h,
        post_json(
            "/im/send",
            serde_json::json!({ "chat_guid": "iMessage;+;gid", "text": "hi" }),
        ),
    )
    .await;
    // The stub cannot do groups, but the request was routed to the group path
    // (a missing `to` did not reject it).
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(body["error"].as_str().unwrap().contains("group"));
}

#[tokio::test]
async fn facetime_link_is_gated_then_names_the_limitation() {
    let h = harness();

    let (status, body) = send(
        &h,
        post_json("/im/facetime-link", serde_json::json!({ "handle": "+1555" })),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"], serde_json::json!("not registered"));

    h.store.set_registered(true, vec![]);
    let (status, body) = send(&h, post_json("/im/facetime-link", serde_json::json!({}))).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(body["error"].as_str().unwrap().contains("FaceTime"));
}

#[tokio::test]
async fn events_stream_emits_ready_and_message_frames() {
    let h = harness();

    // Registered before connecting -> the stream starts with a ready frame.
    h.store
        .set_registered(true, vec!["mailto:me@example.com".into()]);

    let request = get("/im/events");
    let response = h.app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .headers()
            .get("content-type")
            .unwrap()
            .to_str()
            .unwrap(),
        "text/event-stream"
    );

    // Publish a message; read the body until both frames have arrived.
    h.bus.message(MessageEvent {
        guid: "GUID-1".into(),
        chat_guid: "iMessage;-;+15551234567".into(),
        from: "+15551234567".into(),
        to: vec!["me@example.com".into()],
        text: "hello".into(),
        timestamp: 1_700_000_000_000,
        is_group: false,
        sender_name: None,
    });
    // The ready frame is buffered before there is a subscriber, so re-publish
    // nothing; just make sure a second ready is not needed.
    h.bus.publish(Event::Ready);

    let mut body = response.into_body();
    let mut collected = String::new();
    for _ in 0..8 {
        let chunk = tokio::time::timeout(std::time::Duration::from_secs(2), body.frame()).await;
        match chunk {
            Ok(Some(Ok(frame))) => {
                if let Ok(data) = frame.into_data() {
                    collected.push_str(&String::from_utf8_lossy(&data));
                }
                if collected.contains("\"guid\":\"GUID-1\"")
                    && collected.matches("data:").count() >= 2
                {
                    break;
                }
            }
            _ => break,
        }
    }

    assert!(
        collected.contains("{\"type\":\"ready\"}"),
        "got: {collected}"
    );
    assert!(
        collected.contains("\"type\":\"message\""),
        "got: {collected}"
    );
    assert!(
        collected.contains("\"guid\":\"GUID-1\""),
        "got: {collected}"
    );
    assert!(collected.contains("\"is_group\":false"), "got: {collected}");
    assert!(
        collected.contains("\"sender_name\":null"),
        "got: {collected}"
    );
}

/// A backend that reports login diagnostics, to prove `/im/status` surfaces
/// them. The stub always reports the default null/null.
struct DiagnosticBackend {
    inner: StubBackend,
    login: LoginStatus,
}

#[async_trait::async_trait]
impl Backend for DiagnosticBackend {
    fn name(&self) -> &'static str {
        "diagnostic"
    }

    async fn start(&self) -> Result<(), BackendError> {
        self.inner.start().await
    }

    async fn send_message(
        &self,
        to: &str,
        text: &str,
        reply_to: Option<String>,
    ) -> Result<String, BackendError> {
        self.inner.send_message(to, text, reply_to).await
    }

    async fn register_phone(&self, number: &str, signature_hex: &str) -> Result<(), BackendError> {
        self.inner.register_phone(number, signature_hex).await
    }

    async fn apns_token(&self) -> Result<String, BackendError> {
        self.inner.apns_token().await
    }

    fn login_status(&self) -> LoginStatus {
        self.login.clone()
    }
}

#[tokio::test]
async fn status_surfaces_login_diagnostics() {
    let dir = std::env::temp_dir().join(format!("ims-diag-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let bus = EventBus::new();
    let store = StateStore::load(
        dir.join("state.plist"),
        "me@example.com".into(),
        bus.clone(),
    )
    .unwrap();
    let backend: Arc<dyn Backend> = Arc::new(DiagnosticBackend {
        inner: StubBackend::new(store.clone()),
        login: LoginStatus {
            error: Some("This action cannot be completed at this time. (-80045)".into()),
            last_attempt: Some(1_792_000_000),
            two_factor_pending: false,
        },
    });
    let app = api::router(Arc::new(AppState::new(store.clone(), backend, bus.clone())));

    let response = app.oneshot(get("/im/status")).await.unwrap();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        body["login_error"],
        serde_json::json!("This action cannot be completed at this time. (-80045)")
    );
    assert_eq!(body["last_login_attempt"], serde_json::json!(1_792_000_000));

    // The stub default keeps both keys present, as null.
    let h = harness();
    let (_, body) = send(&h, get("/im/status")).await;
    assert!(body.as_object().unwrap().contains_key("login_error"));
    assert!(body.as_object().unwrap().contains_key("last_login_attempt"));
    assert_eq!(body["login_error"], serde_json::json!(null));
    assert_eq!(body["last_login_attempt"], serde_json::json!(null));
}
