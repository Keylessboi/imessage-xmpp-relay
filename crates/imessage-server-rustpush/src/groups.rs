//! Group-chat state transitions: pure functions over the saved chat map.
//!
//! Everything here is deterministic and free of I/O so it can be unit tested
//! without an Apple session. `backend.rs` calls it from the push loop (inbound
//! rename / member / icon changes) and from `Backend::group_op` (outbound).
//!
//! iMessage has no server-side group object: a "group" is a participant list
//! plus a group id (`gid`, our `sender_guid`/`chat_guid`) that every member's
//! device keys the thread on. A rename or a member change is just another
//! message carrying that id, so these helpers only maintain our local copy.

use std::collections::HashMap;

use imessage_server::events::{normalize_handle, strip_handle};
use imessage_server::groups::{GroupEvent, GroupInfo};
use rustpush::{Message, MessageInst};

use crate::backend::SavedChat;

/// iMessage will not let a group shrink below three members (us included);
/// below that it is a plain 1:1 chat.
pub const MIN_GROUP_MEMBERS: usize = 3;

/// Prefix (`tel:`/`mailto:`), dedupe and sort, so two equal member sets compare
/// equal regardless of order.
pub fn canonical(handles: &[String]) -> Vec<String> {
    let mut out: Vec<String> = handles.iter().map(|h| normalize_handle(h)).collect();
    out.sort();
    out.dedup();
    out
}

fn is_me(handle: &str, me: &[String]) -> bool {
    me.iter().any(|m| normalize_handle(m) == handle)
}

/// The member list after adding and removing handles. `me` are our own
/// handles: they are always kept (use [`leave_members`] to leave) and never
/// counted as an addition.
pub fn members_after(
    current: &[String],
    me: &[String],
    add: &[String],
    remove: &[String],
) -> Result<Vec<String>, String> {
    let remove = canonical(remove);
    if remove.iter().any(|h| is_me(h, me)) {
        return Err("use leave to remove this account from a group".into());
    }
    let mut members = canonical(current);
    let before = members.clone();
    let missing: Vec<String> = remove
        .iter()
        .filter(|h| !before.contains(h))
        .map(|h| strip_handle(h))
        .collect();
    if !missing.is_empty() {
        return Err(format!("not in the group: {}", missing.join(", ")));
    }
    members.retain(|m| !remove.contains(m));
    for handle in canonical(add) {
        if !members.contains(&handle) {
            members.push(handle);
        }
    }
    members.sort();
    if members.len() < MIN_GROUP_MEMBERS {
        return Err(format!(
            "a group needs at least {MIN_GROUP_MEMBERS} members including this account"
        ));
    }
    if members == before {
        return Err("no change: the member list is already that".into());
    }
    Ok(members)
}

/// The member list with all of our own handles removed (a "leave").
pub fn leave_members(current: &[String], me: &[String]) -> Vec<String> {
    canonical(current)
        .into_iter()
        .filter(|m| !is_me(m, me))
        .collect()
}

/// `(added, removed)` going from `old` to `new`, as bare handles.
pub fn diff_members(old: &[String], new: &[String]) -> (Vec<String>, Vec<String>) {
    let (old, new) = (canonical(old), canonical(new));
    let added = new
        .iter()
        .filter(|h| !old.contains(h))
        .map(|h| strip_handle(h))
        .collect();
    let removed = old
        .iter()
        .filter(|h| !new.contains(h))
        .map(|h| strip_handle(h))
        .collect();
    (added, removed)
}

impl SavedChat {
    /// The wire-independent view the HTTP API returns.
    pub fn info(&self, chat_guid: &str) -> GroupInfo {
        GroupInfo {
            chat_guid: chat_guid.to_string(),
            name: self.name.clone(),
            participants: self.participants.iter().map(|p| strip_handle(p)).collect(),
            group_version: self.group_version,
        }
    }
}

/// Apply an inbound rename / member change / icon change to the chat map and
/// describe it as an SSE event. `None` for every other message, and for a
/// change to a conversation without a group id (nothing to key it on).
///
/// Returns the event plus whether the persisted map changed.
pub fn apply_inbound(
    chats: &mut HashMap<String, SavedChat>,
    inst: &MessageInst,
) -> Option<(GroupEvent, bool)> {
    if !matches!(
        inst.message,
        Message::RenameMessage(_) | Message::ChangeParticipants(_) | Message::IconChange(_)
    ) {
        return None;
    }
    let conversation = inst.conversation.as_ref()?;
    let chat_guid = conversation.sender_guid.clone()?;
    let from = inst.sender.as_deref().map(strip_handle).unwrap_or_default();
    let known = chats.get(&chat_guid).cloned();
    let source = canonical(&conversation.participants);
    let mut event = GroupEvent {
        guid: inst.id.clone(),
        chat_guid: chat_guid.clone(),
        from,
        timestamp: inst.sent_timestamp as i64,
        ..Default::default()
    };

    let entry = chats.entry(chat_guid).or_default();
    if entry.participants.is_empty() {
        entry.participants = source.clone();
    }
    let changed = match &inst.message {
        Message::RenameMessage(rename) => {
            event.kind = "rename".into();
            let changed = entry.name.as_deref() != Some(rename.new_name.as_str());
            entry.name = Some(rename.new_name.clone());
            changed
        }
        Message::ChangeParticipants(change) => {
            event.kind = "participants".into();
            let old = known
                .as_ref()
                .map(|c| c.participants.clone())
                .filter(|p| !p.is_empty())
                .unwrap_or(source);
            let new = canonical(&change.new_participants);
            let (added, removed) = diff_members(&old, &new);
            event.added = added;
            event.removed = removed;
            let changed = entry.participants != new || entry.group_version != change.group_version;
            entry.participants = new;
            entry.group_version = entry.group_version.max(change.group_version);
            changed
        }
        Message::IconChange(_) => {
            event.kind = "icon".into();
            false
        }
        _ => unreachable!("filtered above"),
    };
    event.name = entry.name.clone();
    event.participants = entry.participants.iter().map(|p| strip_handle(p)).collect();
    Some((event, changed))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustpush::{ChangeParticipantMessage, ConversationData, IconChangeMessage, RenameMessage};

    fn v(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn me() -> Vec<String> {
        v(&["tel:+1000"])
    }

    #[test]
    fn canonical_prefixes_sorts_dedups() {
        assert_eq!(
            canonical(&v(&["+2", "tel:+2", "a@b", "tel:+1"])),
            v(&["mailto:a@b", "tel:+1", "tel:+2"])
        );
    }

    #[test]
    fn members_after_adds_and_removes() {
        let cur = v(&["tel:+1000", "tel:+1", "tel:+2"]);
        let added = members_after(&cur, &me(), &v(&["+3"]), &[]).unwrap();
        assert_eq!(added, v(&["tel:+1", "tel:+1000", "tel:+2", "tel:+3"]));
        let removed = members_after(&added, &me(), &[], &v(&["tel:+3"])).unwrap();
        assert_eq!(removed, v(&["tel:+1", "tel:+1000", "tel:+2"]));
    }

    #[test]
    fn members_after_rejects_bad_changes() {
        let cur = v(&["tel:+1000", "tel:+1", "tel:+2"]);
        assert!(members_after(&cur, &me(), &[], &v(&["+1"])).is_err()); // below 3
        assert!(members_after(&cur, &me(), &[], &v(&["+1000"])).is_err()); // self
        assert!(members_after(&cur, &me(), &[], &v(&["+9"])).is_err()); // not a member
        assert!(members_after(&cur, &me(), &v(&["+1"]), &[]).is_err()); // no change
    }

    #[test]
    fn leave_drops_only_us() {
        let cur = v(&["tel:+1000", "tel:+1", "tel:+2"]);
        assert_eq!(leave_members(&cur, &me()), v(&["tel:+1", "tel:+2"]));
    }

    #[test]
    fn diff_is_bare_and_directional() {
        let (added, removed) = diff_members(&v(&["tel:+1", "tel:+2"]), &v(&["tel:+2", "mailto:a@b"]));
        assert_eq!(added, v(&["a@b"]));
        assert_eq!(removed, v(&["+1"]));
    }

    fn inst(message: Message) -> MessageInst {
        MessageInst::new(
            ConversationData {
                participants: v(&["tel:+1000", "tel:+1", "tel:+2"]),
                cv_name: Some("Old".into()),
                sender_guid: Some("GID".into()),
                after_guid: None,
            },
            "tel:+1",
            message,
        )
    }

    #[test]
    fn inbound_rename_updates_name() {
        let mut chats = HashMap::new();
        let (event, changed) = apply_inbound(
            &mut chats,
            &inst(Message::RenameMessage(RenameMessage { new_name: "New".into() })),
        )
        .unwrap();
        assert!(changed);
        assert_eq!(event.kind, "rename");
        assert_eq!(event.chat_guid, "GID");
        assert_eq!(event.from, "+1");
        assert_eq!(event.name.as_deref(), Some("New"));
        assert_eq!(chats["GID"].name.as_deref(), Some("New"));
        assert_eq!(event.participants, v(&["+1", "+1000", "+2"]));
    }

    #[test]
    fn inbound_member_change_diffs_against_known_members() {
        let mut chats = HashMap::new();
        chats.insert(
            "GID".to_string(),
            SavedChat {
                participants: canonical(&v(&["tel:+1000", "tel:+1", "tel:+2"])),
                ..Default::default()
            },
        );
        let change = Message::ChangeParticipants(ChangeParticipantMessage {
            new_participants: v(&["tel:+1000", "tel:+1", "tel:+3"]),
            group_version: 4,
        });
        let (event, changed) = apply_inbound(&mut chats, &inst(change)).unwrap();
        assert!(changed);
        assert_eq!(event.kind, "participants");
        assert_eq!(event.added, v(&["+3"]));
        assert_eq!(event.removed, v(&["+2"]));
        assert_eq!(chats["GID"].group_version, 4);
        assert_eq!(event.participants, v(&["+1", "+1000", "+3"]));
    }

    #[test]
    fn inbound_icon_is_reported_not_stored() {
        let mut chats = HashMap::new();
        let icon = Message::IconChange(IconChangeMessage { file: None, group_version: 2 });
        let (event, changed) = apply_inbound(&mut chats, &inst(icon)).unwrap();
        assert_eq!(event.kind, "icon");
        assert!(!changed);
    }

    #[test]
    fn ordinary_messages_are_not_group_events() {
        let mut chats = HashMap::new();
        assert!(apply_inbound(&mut chats, &inst(Message::Delivered)).is_none());
        assert!(chats.is_empty());
    }
}
