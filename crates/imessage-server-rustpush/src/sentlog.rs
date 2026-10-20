//! Bounded record of recent outbound sends, so an IDS error for a sent guid can
//! trigger exactly one resend. Pure bookkeeping, no Apple dependency.

use std::collections::{HashMap, VecDeque};

use rustpush::ConversationData;

/// How many sends are remembered before the oldest is evicted.
pub const MAX_SENT: usize = 256;

#[derive(Clone)]
pub struct SentRecord {
    pub conversation: ConversationData,
    pub text: String,
    pub reply_to: Option<String>,
    pub chat_guid: Option<String>,
    pub retried: bool,
}

#[derive(Default)]
pub struct SentLog {
    order: VecDeque<String>,
    map: HashMap<String, SentRecord>,
}

/// Does this IDS error text mean "stale identity, a refresh may fix it"?
pub fn is_identity_error(status_str: &str) -> bool {
    status_str.contains("messageprotection")
}

impl SentLog {
    pub fn record(&mut self, guid: String, rec: SentRecord) {
        if self.map.insert(guid.clone(), rec).is_none() {
            self.order.push_back(guid);
        }
        while self.order.len() > MAX_SENT {
            if let Some(old) = self.order.pop_front() {
                self.map.remove(&old);
            }
        }
    }

    /// If `guid` was sent by us, has not been retried, and `status_str` is an
    /// identity error: mark it retried and return the record to resend.
    pub fn take_retry(&mut self, guid: &str, status_str: &str) -> Option<SentRecord> {
        if !is_identity_error(status_str) {
            return None;
        }
        let rec = self.map.get_mut(guid)?;
        if rec.retried {
            return None;
        }
        rec.retried = true;
        Some(rec.clone())
    }

    /// Record the resend under its new guid, already marked retried, so a
    /// second error for it can never loop.
    pub fn record_retry(&mut self, new_guid: String, mut rec: SentRecord) {
        rec.retried = true;
        self.record(new_guid, rec);
    }

    #[cfg(test)]
    pub fn len(&self) -> usize {
        self.map.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(text: &str) -> SentRecord {
        SentRecord {
            conversation: ConversationData {
                participants: vec!["tel:+15550000000".into()],
                cv_name: None,
                sender_guid: None,
                after_guid: None,
            },
            text: text.into(),
            reply_to: None,
            chat_guid: None,
            retried: false,
        }
    }

    const MP: &str = "ec-com.apple.messageprotection-802";

    #[test]
    fn retries_once_for_messageprotection() {
        let mut log = SentLog::default();
        log.record("a".into(), rec("hi"));
        let r = log.take_retry("a", MP).expect("first error retries");
        assert_eq!(r.text, "hi");
        assert!(log.take_retry("a", MP).is_none(), "second error must not");
    }

    #[test]
    fn resend_guid_never_loops() {
        let mut log = SentLog::default();
        log.record("a".into(), rec("hi"));
        let r = log.take_retry("a", MP).unwrap();
        log.record_retry("b".into(), r);
        assert!(log.take_retry("b", MP).is_none());
    }

    #[test]
    fn other_status_and_unknown_guid_do_not_retry() {
        let mut log = SentLog::default();
        log.record("a".into(), rec("hi"));
        assert!(log.take_retry("a", "failed to receive our message").is_none());
        assert!(log.take_retry("zzz", MP).is_none());
        // a non-matching error must not consume the retry
        assert!(log.take_retry("a", MP).is_some());
    }

    #[test]
    fn evicts_oldest() {
        let mut log = SentLog::default();
        for i in 0..(MAX_SENT + 10) {
            log.record(format!("g{i}"), rec("x"));
        }
        assert_eq!(log.len(), MAX_SENT);
        assert!(log.take_retry("g0", MP).is_none());
        assert!(log.take_retry(&format!("g{}", MAX_SENT + 9), MP).is_some());
    }
}
