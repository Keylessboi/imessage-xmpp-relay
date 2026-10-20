//! Inbound attachments: MMCS download to disk and the `attachment` SSE event.
//!
//! The push loop calls [`Files::spawn_downloads`] for every inbound message; the
//! download runs in its own task so a slow MMCS fetch never stalls receipts or
//! other messages. All trust decisions (name, mime, size, quota) live in
//! `imessage_server::attach`; this file only bridges rustpush to it.
//!
//! UNPROVEN against real Apple: whether MMCS accepts our synthetic identity for
//! a download has not been exercised (no live inbound attachment was available
//! while writing this).

use std::sync::Arc;

use anyhow::{anyhow, Result};
use imessage_server::attach::{store_with, Store};
use imessage_server::config::Config;
use imessage_server::events::{AttachmentEvent, Event, EventBus};
use imessage_server::files::{unix_now, Signer};
use rustpush::{
    APSConnection, Attachment, AttachmentType, IndexedMessagePart, MMCSFile, Message, MessageInst,
    MessagePart, MessageParts, MessageType, NormalMessage,
};

pub struct Files {
    store: Store,
    signer: Signer,
    ttl_secs: i64,
}

impl Files {
    /// `None` when attachments are disabled (no HMAC key configured).
    pub fn from_config(config: &Config) -> Option<Arc<Self>> {
        let f = config.files.as_ref()?;
        match Store::open(f.dir.clone(), f.limits.clone()) {
            Ok(store) => Some(Arc::new(Self {
                store,
                signer: Signer::new(f.hmac_key.clone()),
                ttl_secs: f.url_ttl_secs,
            })),
            Err(e) => {
                tracing::error!("attachments disabled: cannot open the attachment dir: {e:#}");
                None
            }
        }
    }

    /// Start one download task per attachment of an inbound message.
    pub fn spawn_downloads(
        self: &Arc<Self>,
        connection: &APSConnection,
        bus: &EventBus,
        inst: &MessageInst,
        guid: String,
        chat_guid: String,
        from: String,
    ) {
        for attachment in inbound_attachments(inst) {
            let me = self.clone();
            let connection = connection.clone();
            let bus = bus.clone();
            let (guid, chat_guid, from) = (guid.clone(), chat_guid.clone(), from.clone());
            tokio::spawn(async move {
                match me.fetch(&connection, &attachment).await {
                    Ok((id, meta)) => {
                        tracing::info!(%guid, %id, size = meta.size, mime = %meta.mime, "attachment stored");
                        bus.publish(Event::Attachment(AttachmentEvent {
                            guid,
                            chat_guid,
                            from,
                            path: me.signer.signed_path(&id, unix_now(), me.ttl_secs),
                            id,
                            name: meta.name,
                            mime: meta.mime,
                            size: meta.size,
                        }));
                    }
                    Err(e) => tracing::warn!(%guid, "attachment download failed: {e:#}"),
                }
            });
        }
    }

    async fn fetch(
        &self,
        connection: &APSConnection,
        attachment: &Attachment,
    ) -> Result<(String, imessage_server::attach::Meta)> {
        let declared = attachment.get_size() as u64;
        store_with(
            &self.store,
            &attachment.name,
            &attachment.mime,
            declared,
            unix_now(),
            |mut writer| async move {
                attachment
                    .get_attachment(&***connection, &mut writer, |_done, _total| {})
                    .await
                    .map_err(|e| anyhow!("MMCS download failed: {e}"))?;
                Ok(writer)
            },
        )
        .await
    }
}

/// The attachment parts of an inbound normal message (clones; they are small
/// descriptors, the bytes are fetched later).
pub fn inbound_attachments(inst: &MessageInst) -> Vec<Attachment> {
    let Message::Message(normal) = &inst.message else {
        return Vec::new();
    };
    normal
        .parts
        .0
        .iter()
        .filter_map(|p| match &p.part {
            MessagePart::Attachment(a) => Some(a.clone()),
            _ => None,
        })
        .collect()
}

/// Whether the descriptor carries inline bytes rather than an MMCS reference
/// (small attachments); both go through the same store.
#[allow(dead_code)]
pub fn is_inline(a: &Attachment) -> bool {
    matches!(a.a_type, AttachmentType::Inline(_))
}

/// A normal iMessage whose only part is `attachment` (no caption).
pub fn attachment_message(attachment: Attachment) -> Message {
    let mut normal = NormalMessage::new(String::new(), MessageType::IMessage);
    normal.parts = MessageParts(vec![IndexedMessagePart {
        part: MessagePart::Attachment(attachment),
        idx: Some(0),
        ext: None,
    }]);
    Message::Message(normal)
}

/// Encrypt and upload the file at `path` to MMCS and describe it as a rustpush
/// `Attachment`. `name` must already be sanitised and `mime` sniffed.
///
/// Two passes over the file (hash/chunk, then upload), so it is opened twice;
/// rustpush reads through blocking `Read`, hence the files rather than memory.
///
/// UNPROVEN against real Apple: whether MMCS accepts an upload from our
/// synthetic identity (APS c=150 + HTTP PUT, no iCloud token) is untested.
pub async fn upload(
    connection: &APSConnection,
    path: &std::path::Path,
    name: &str,
    mime: &str,
) -> Result<Attachment> {
    let first = std::fs::File::open(path)?;
    let prepared = MMCSFile::prepare_put(first)
        .await
        .map_err(|e| anyhow!("preparing the MMCS upload failed: {e}"))?;
    let second = std::fs::File::open(path)?;
    let uti = imessage_server::attach::uti_for(mime);
    Attachment::new_mmcs(&**connection, &prepared, second, mime, uti, name, |_done, _total| {})
        .await
        .map_err(|e| anyhow!("MMCS upload failed: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn att() -> Attachment {
        Attachment {
            a_type: AttachmentType::Inline(b"abc".to_vec()),
            part: 0,
            uti_type: "public.png".into(),
            mime: "image/png".into(),
            name: "a.png".into(),
            iris: false,
        }
    }

    #[test]
    fn outbound_message_has_one_attachment_part() {
        let Message::Message(n) = attachment_message(att()) else { panic!("not a normal message") };
        assert_eq!(n.parts.0.len(), 1);
        assert!(matches!(&n.parts.0[0].part, MessagePart::Attachment(a) if a.name == "a.png"));
        assert!(n.reply_guid.is_none());
    }

    #[test]
    fn inbound_attachments_lists_attachment_parts_only() {
        use rustpush::ConversationData;
        let inst = MessageInst::new(
            ConversationData {
                participants: vec!["tel:+1".into()],
                cv_name: None,
                sender_guid: None,
                after_guid: None,
            },
            "tel:+1",
            attachment_message(att()),
        );
        let got = inbound_attachments(&inst);
        assert_eq!(got.len(), 1);
        assert!(is_inline(&got[0]));
        let text = MessageInst::new(
            ConversationData { participants: vec![], cv_name: None, sender_guid: None, after_guid: None },
            "tel:+1",
            Message::Message(NormalMessage::new("hi".into(), MessageType::IMessage)),
        );
        assert!(inbound_attachments(&text).is_empty());
    }
}
