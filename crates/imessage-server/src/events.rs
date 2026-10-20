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

/// A delivered (`delivered`) or read (`read`) receipt. `guid` is the message the
/// peer is receipting (one of ours); the wire carries no conversation for these,
/// so `chat_guid` is the 1:1 form for `from`.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct ReceiptEvent {
    pub guid: String,
    pub chat_guid: String,
    pub from: String,
    pub timestamp: i64,
}

/// Typing indicator (`typing`). `typing=false` is "stopped typing".
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct TypingEvent {
    pub chat_guid: String,
    pub from: String,
    pub typing: bool,
    pub timestamp: i64,
}

/// A tapback (`reaction`). `reaction` is heart|like|dislike|laugh|emphasize|
/// question|emoji|sticker; `emoji` is set for `emoji`. `enable=false` removes it.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct ReactionEvent {
    /// guid of the reaction message itself
    pub guid: String,
    /// guid of the message being reacted to
    pub to_guid: String,
    pub to_part: Option<u64>,
    pub chat_guid: String,
    pub from: String,
    pub reaction: String,
    pub emoji: Option<String>,
    pub enable: bool,
    pub timestamp: i64,
}

/// An edit (`edit`) of an earlier message: `guid` is the edited message.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct EditEvent {
    pub guid: String,
    pub part: u64,
    pub chat_guid: String,
    pub from: String,
    pub text: String,
    pub timestamp: i64,
}

/// An unsend (`unsend`) of an earlier message: `guid` is the unsent message.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct UnsendEvent {
    pub guid: String,
    pub part: u64,
    pub chat_guid: String,
    pub from: String,
    pub timestamp: i64,
}

/// One attachment (or sticker) of an inbound message. The bytes are not
/// downloaded; this is enough for a readable placeholder.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct AttachmentInfo {
    pub name: String,
    pub mime: String,
    pub sticker: bool,
}

/// Extra facts about a `message` that its frozen shape cannot carry (reply
/// target, attachments). Published immediately BEFORE the `message` with the
/// same `guid`; a consumer that ignores it loses nothing.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct MessageMetaEvent {
    pub guid: String,
    pub chat_guid: String,
    pub from: String,
    pub reply_to: Option<String>,
    pub reply_part: Option<String>,
    pub attachments: Vec<AttachmentInfo>,
}

/// A downloaded inbound attachment, ready to fetch from the file listener.
/// Published AFTER the `message_meta` that announced it. `path` is a signed,
/// short-lived `/dl/<id>?exp=&sig=` path to append to the public base URL.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct AttachmentEvent {
    /// guid of the message that carried the attachment
    pub guid: String,
    pub chat_guid: String,
    pub from: String,
    /// server-minted storage id (32 hex), not the sender's
    pub id: String,
    /// sanitised file name
    pub name: String,
    /// sniffed mime type
    pub mime: String,
    pub size: u64,
    pub path: String,
}

/// Anything that can appear on `/im/events`. Serialises with an internal tag:
/// `{"type":"ready"}` or `{"type":"message",...}`. Every variant after
/// `Message` is additive: the `ready` and `message` shapes are frozen.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Event {
    Ready,
    Message(MessageEvent),
    Delivered(ReceiptEvent),
    Read(ReceiptEvent),
    Typing(TypingEvent),
    Reaction(ReactionEvent),
    Edit(EditEvent),
    Unsend(UnsendEvent),
    #[serde(rename = "message_meta")]
    MessageMeta(MessageMetaEvent),
    Attachment(AttachmentEvent),
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
    fn additive_event_tags() {
        let receipt = ReceiptEvent {
            guid: "G".into(),
            chat_guid: "iMessage;-;+1".into(),
            from: "+1".into(),
            timestamp: 5,
        };
        let v = serde_json::to_value(Event::Delivered(receipt.clone())).unwrap();
        assert_eq!(v["type"], "delivered");
        assert_eq!(v["guid"], "G");
        assert_eq!(serde_json::to_value(Event::Read(receipt)).unwrap()["type"], "read");
        let meta = Event::MessageMeta(MessageMetaEvent {
            guid: "G".into(),
            chat_guid: "c".into(),
            from: "+1".into(),
            reply_to: Some("R".into()),
            reply_part: Some("0".into()),
            attachments: vec![AttachmentInfo {
                name: "a.png".into(),
                mime: "image/png".into(),
                sticker: true,
            }],
        });
        let att = Event::Attachment(AttachmentEvent {
            guid: "G".into(),
            chat_guid: "c".into(),
            from: "+1".into(),
            id: "a".repeat(32),
            name: "a.png".into(),
            mime: "image/png".into(),
            size: 3,
            path: "/dl/x".into(),
        });
        let a = serde_json::to_value(att).unwrap();
        assert_eq!(a["type"], "attachment");
        assert_eq!(a["size"], 3);
        let v = serde_json::to_value(meta).unwrap();
        assert_eq!(v["type"], "message_meta");
        assert_eq!(v["attachments"][0]["sticker"], true);
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
