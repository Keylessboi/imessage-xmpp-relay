//! SSE event bus and the frozen `/im/events` payload shapes.

use serde::Serialize;
use tokio::sync::broadcast;

/// One inbound iMessage, exactly as it appears on the SSE stream:
/// `{"type":"message","guid":...,"chat_guid":...,"from":...,"to":[...],...}`
#[derive(Clone, Debug, Serialize)]
pub struct MessageEvent {
    pub guid: String,
    pub chat_guid: String,
    pub from: String,
    pub to: Vec<String>,
    pub text: String,
    pub timestamp: i64,
    pub is_group: bool,
    pub sender_name: Option<String>,
}

/// Anything that can appear on `/im/events`. Serialises with an internal tag:
/// `{"type":"ready"}` or `{"type":"message",...}`.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Event {
    Ready,
    Message(MessageEvent),
}

/// Fan-out bus feeding every connected `/im/events` listener.
#[derive(Clone)]
pub struct EventBus {
    tx: broadcast::Sender<Event>,
}

impl Default for EventBus {
    fn default() -> Self {
        Self::new()
    }
}

impl EventBus {
    pub fn new() -> Self {
        let (tx, _) = broadcast::channel(1024);
        Self { tx }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.tx.subscribe()
    }

    pub fn publish(&self, event: Event) {
        // No subscribers is not an error.
        let _ = self.tx.send(event);
    }

    pub fn ready(&self) {
        self.publish(Event::Ready);
    }

    pub fn message(&self, message: MessageEvent) {
        self.publish(Event::Message(message));
    }
}

/// `tel:+1555...` / `mailto:a@b` -> bare handle.
pub fn strip_handle(handle: &str) -> String {
    handle
        .strip_prefix("tel:")
        .or_else(|| handle.strip_prefix("mailto:"))
        .unwrap_or(handle)
        .to_string()
}

/// Bare handle -> `tel:` or `mailto:` prefixed handle, as IDS expects.
pub fn normalize_handle(handle: &str) -> String {
    let bare = strip_handle(handle);
    if bare.contains('@') {
        format!("mailto:{bare}")
    } else {
        format!("tel:{bare}")
    }
}

fn fnv1a_hex(bytes: &[u8]) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        hash ^= *b as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{hash:016x}")
}

/// Stable chat identifier for a conversation. 1:1 chats use the peer handle
/// (BlueBubbles style `iMessage;-;<peer>`); groups hash the sorted participant
/// set, so the same group always maps to the same id even without a sender guid.
pub fn chat_guid_for(from: &str, to: &[String]) -> String {
    if to.len() <= 1 {
        let peer = to.first().cloned().unwrap_or_else(|| from.to_string());
        format!("iMessage;-;{peer}")
    } else {
        let mut parts: Vec<String> = to.to_vec();
        parts.push(from.to_string());
        parts.sort();
        parts.dedup();
        format!("iMessage;+;{}", fnv1a_hex(parts.join(",").as_bytes()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_shape() {
        assert_eq!(
            serde_json::to_string(&Event::Ready).unwrap(),
            r#"{"type":"ready"}"#
        );
    }

    #[test]
    fn message_shape_is_frozen() {
        let ev = Event::Message(MessageEvent {
            guid: "G".into(),
            chat_guid: "iMessage;-;+15551234567".into(),
            from: "+15551234567".into(),
            to: vec!["me@example.com".into()],
            text: "hi".into(),
            timestamp: 1234,
            is_group: false,
            sender_name: None,
        });
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&ev).unwrap()).unwrap();
        assert_eq!(v["type"], "message");
        assert_eq!(v["guid"], "G");
        assert_eq!(v["chat_guid"], "iMessage;-;+15551234567");
        assert_eq!(v["from"], "+15551234567");
        assert_eq!(v["to"][0], "me@example.com");
        assert_eq!(v["text"], "hi");
        assert_eq!(v["timestamp"], 1234);
        assert_eq!(v["is_group"], false);
        assert!(v["sender_name"].is_null());
    }

    #[test]
    fn handles() {
        assert_eq!(strip_handle("tel:+1555"), "+1555");
        assert_eq!(strip_handle("mailto:a@b"), "a@b");
        assert_eq!(normalize_handle("+1555"), "tel:+1555");
        assert_eq!(normalize_handle("mailto:a@b"), "mailto:a@b");
        assert_eq!(normalize_handle("a@b"), "mailto:a@b");
    }

    #[test]
    fn chat_guid_is_stable_for_groups() {
        let a = chat_guid_for("+1", &["+2".into(), "+3".into()]);
        let b = chat_guid_for("+1", &["+3".into(), "+2".into()]);
        assert_eq!(a, b);
        assert_eq!(chat_guid_for("+1", &["+2".into()]), "iMessage;-;+2");
    }
}
