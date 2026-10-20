//! Receipts, typing, tapbacks, edits, unsends and attachment placeholders.
//!
//! Pure mapping between rustpush's `Message` variants and the additive SSE
//! events / outbound `Action`s. Kept out of backend.rs so the existing text path
//! is untouched. Nothing here talks to Apple.

use anyhow::{anyhow, Result};
use imessage_server::backend::Action;
use imessage_server::events::{
    chat_guid_for, strip_handle, AttachmentInfo, EditEvent, Event, MessageMetaEvent,
    ReactionEvent, ReceiptEvent, TypingEvent, UnsendEvent,
};
use rustpush::{
    EditMessage, Message, MessageInst, MessagePart, MessageType, NormalMessage, PartExtension,
    ReactMessage, ReactMessageType, Reaction, UnsendMessage,
};

/// (sender handle, chat_guid) exactly as `message_event` derives them.
fn context(inst: &MessageInst) -> (String, String) {
    let from = inst.sender.as_deref().map(strip_handle).unwrap_or_default();
    let to: Vec<String> = inst
        .conversation
        .as_ref()
        .map(|c| {
            c.participants
                .iter()
                .map(|p| strip_handle(p))
                .filter(|p| *p != from)
                .collect()
        })
        .unwrap_or_default();
    let chat_guid = inst
        .conversation
        .as_ref()
        .and_then(|c| c.sender_guid.clone())
        .unwrap_or_else(|| chat_guid_for(&from, &to));
    (from, chat_guid)
}

/// Wire name (and emoji) of a tapback; None for sticker/extension reactions,
/// which are surfaced as the generic name only.
pub fn reaction_name(reaction: &Reaction) -> (&'static str, Option<String>) {
    match reaction {
        Reaction::Heart => ("heart", None),
        Reaction::Like => ("like", None),
        Reaction::Dislike => ("dislike", None),
        Reaction::Laugh => ("laugh", None),
        Reaction::Emphasize => ("emphasize", None),
        Reaction::Question => ("question", None),
        Reaction::Emoji(e) => ("emoji", Some(e.clone())),
        Reaction::Sticker { .. } => ("sticker", None),
    }
}

pub fn parse_reaction(name: &str, emoji: Option<&str>) -> Option<Reaction> {
    Some(match name {
        "heart" => Reaction::Heart,
        "like" => Reaction::Like,
        "dislike" => Reaction::Dislike,
        "laugh" => Reaction::Laugh,
        "emphasize" => Reaction::Emphasize,
        "question" => Reaction::Question,
        "emoji" => Reaction::Emoji(emoji.filter(|e| !e.is_empty())?.to_string()),
        _ => return None,
    })
}

/// The additive events an inbound message produces, in publish order. The
/// regular `message` event is NOT built here (see `message_event`); a
/// `message_meta` is returned first so it precedes its message.
pub fn extra_events(inst: &MessageInst) -> Vec<Event> {
    let (from, chat_guid) = context(inst);
    let timestamp = inst.sent_timestamp as i64;
    match &inst.message {
        Message::Delivered => vec![Event::Delivered(ReceiptEvent {
            guid: inst.id.clone(),
            chat_guid,
            from,
            timestamp,
        })],
        Message::Read => vec![Event::Read(ReceiptEvent {
            guid: inst.id.clone(),
            chat_guid,
            from,
            timestamp,
        })],
        Message::Typing(typing, _) => vec![Event::Typing(TypingEvent {
            chat_guid,
            from,
            typing: *typing,
            timestamp,
        })],
        Message::React(react) => match &react.reaction {
            ReactMessageType::React { reaction, enable } => {
                let (name, emoji) = reaction_name(reaction);
                vec![Event::Reaction(ReactionEvent {
                    guid: inst.id.clone(),
                    to_guid: react.to_uuid.clone(),
                    to_part: react.to_part,
                    chat_guid,
                    from,
                    reaction: name.to_string(),
                    emoji,
                    enable: *enable,
                    timestamp,
                })]
            }
            // Apps/balloons riding on the react channel: not a tapback.
            ReactMessageType::Extension { .. } => Vec::new(),
        },
        Message::Edit(edit) => vec![Event::Edit(EditEvent {
            guid: edit.tuuid.clone(),
            part: edit.edit_part,
            chat_guid,
            from,
            text: edit.new_parts.raw_text(),
            timestamp,
        })],
        Message::Unsend(unsend) => vec![Event::Unsend(UnsendEvent {
            guid: unsend.tuuid.clone(),
            part: unsend.edit_part,
            chat_guid,
            from,
            timestamp,
        })],
        Message::Message(normal) => message_meta(inst, normal, chat_guid, from)
            .map(Event::MessageMeta)
            .into_iter()
            .collect(),
        _ => Vec::new(),
    }
}

fn message_meta(
    inst: &MessageInst,
    normal: &NormalMessage,
    chat_guid: String,
    from: String,
) -> Option<MessageMetaEvent> {
    let attachments: Vec<AttachmentInfo> = normal
        .parts
        .0
        .iter()
        .filter_map(|p| match &p.part {
            MessagePart::Attachment(a) => Some(AttachmentInfo {
                name: a.name.clone(),
                mime: a.mime.clone(),
                sticker: matches!(p.ext, Some(PartExtension::Sticker { .. })),
            }),
            _ => None,
        })
        .collect();
    if normal.reply_guid.is_none() && attachments.is_empty() {
        return None;
    }
    Some(MessageMetaEvent {
        guid: inst.id.clone(),
        chat_guid,
        from,
        reply_to: normal.reply_guid.clone(),
        reply_part: normal.reply_part.clone(),
        attachments,
    })
}

/// The rustpush `Message` for an outbound action. For `Read` the caller must
/// also set `inst.id` to the receipted guid (see `message_id_override`).
pub fn build_message(action: &Action) -> Result<Message> {
    Ok(match action {
        Action::Read { .. } => Message::Read,
        Action::Typing { typing } => Message::Typing(*typing, None),
        Action::React { guid, part, reaction, emoji, enable, to_text } => {
            let reaction = parse_reaction(reaction, emoji.as_deref())
                .ok_or_else(|| anyhow!("unknown reaction `{reaction}`"))?;
            Message::React(ReactMessage {
                to_uuid: guid.clone(),
                to_part: *part,
                reaction: ReactMessageType::React { reaction, enable: *enable },
                to_text: to_text.clone(),
                embedded_profile: None,
            })
        }
        Action::Edit { guid, part, text } => Message::Edit(EditMessage {
            tuuid: guid.clone(),
            edit_part: *part,
            new_parts: NormalMessage::new(text.clone(), MessageType::IMessage).parts,
        }),
        Action::Unsend { guid, part } => Message::Unsend(UnsendMessage {
            tuuid: guid.clone(),
            edit_part: *part,
        }),
    })
}

/// A read receipt is sent under the receipted message's own id.
pub fn message_id_override(action: &Action) -> Option<&str> {
    match action {
        Action::Read { guid } => Some(guid),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustpush::ConversationData;

    fn inst(message: Message) -> MessageInst {
        MessageInst::new(
            ConversationData {
                participants: vec!["tel:+15551234567".into(), "mailto:me@example.com".into()],
                cv_name: None,
                sender_guid: None,
                after_guid: None,
            },
            "tel:+15551234567",
            message,
        )
    }

    #[test]
    fn receipts_use_the_receipted_guid_and_peer() {
        let mut i = inst(Message::Read);
        i.id = "GUID-1".into();
        match &extra_events(&i)[..] {
            [Event::Read(r)] => {
                assert_eq!(r.guid, "GUID-1");
                assert_eq!(r.from, "+15551234567");
                assert_eq!(r.chat_guid, "iMessage;-;me@example.com");
            }
            other => panic!("unexpected {other:?}"),
        }
        assert!(matches!(&extra_events(&inst(Message::Delivered))[..], [Event::Delivered(_)]));
    }

    #[test]
    fn typing_maps_both_states() {
        for on in [true, false] {
            match &extra_events(&inst(Message::Typing(on, None)))[..] {
                [Event::Typing(t)] => assert_eq!(t.typing, on),
                other => panic!("unexpected {other:?}"),
            }
        }
    }

    #[test]
    fn tapback_maps_to_a_reaction_event() {
        let react = Message::React(ReactMessage {
            to_uuid: "TARGET".into(),
            to_part: Some(0),
            reaction: ReactMessageType::React { reaction: Reaction::Emoji("🎉".into()), enable: false },
            to_text: String::new(),
            embedded_profile: None,
        });
        match &extra_events(&inst(react))[..] {
            [Event::Reaction(r)] => {
                assert_eq!(r.to_guid, "TARGET");
                assert_eq!(r.reaction, "emoji");
                assert_eq!(r.emoji.as_deref(), Some("🎉"));
                assert!(!r.enable);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn edit_and_unsend_target_the_original_guid() {
        let edit = Message::Edit(EditMessage {
            tuuid: "ORIG".into(),
            edit_part: 1,
            new_parts: NormalMessage::new("fixed".into(), MessageType::IMessage).parts,
        });
        match &extra_events(&inst(edit))[..] {
            [Event::Edit(e)] => {
                assert_eq!((e.guid.as_str(), e.part, e.text.as_str()), ("ORIG", 1, "fixed"));
            }
            other => panic!("unexpected {other:?}"),
        }
        let unsend = Message::Unsend(UnsendMessage { tuuid: "ORIG".into(), edit_part: 0 });
        assert!(matches!(&extra_events(&inst(unsend))[..], [Event::Unsend(u)] if u.guid == "ORIG"));
    }

    #[test]
    fn plain_text_has_no_meta_but_a_reply_does() {
        let plain = inst(Message::Message(NormalMessage::new("hi".into(), MessageType::IMessage)));
        assert!(extra_events(&plain).is_empty());
        let mut reply = NormalMessage::new("yes".into(), MessageType::IMessage);
        reply.reply_guid = Some("PARENT".into());
        reply.reply_part = Some("0".into());
        match &extra_events(&inst(Message::Message(reply)))[..] {
            [Event::MessageMeta(m)] => {
                assert_eq!(m.reply_to.as_deref(), Some("PARENT"));
                assert!(m.attachments.is_empty());
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn reaction_names_round_trip() {
        for name in ["heart", "like", "dislike", "laugh", "emphasize", "question"] {
            let r = parse_reaction(name, None).unwrap();
            assert_eq!(reaction_name(&r).0, name);
        }
        assert!(parse_reaction("emoji", None).is_none());
        assert!(parse_reaction("nope", None).is_none());
    }

    #[test]
    fn outbound_actions_build_messages() {
        assert!(matches!(build_message(&Action::Read { guid: "G".into() }).unwrap(), Message::Read));
        assert_eq!(message_id_override(&Action::Read { guid: "G".into() }), Some("G"));
        assert_eq!(message_id_override(&Action::Typing { typing: true }), None);
        let edit = build_message(&Action::Edit { guid: "G".into(), part: 0, text: "x".into() }).unwrap();
        assert!(matches!(edit, Message::Edit(e) if e.tuuid == "G"));
    }
}
