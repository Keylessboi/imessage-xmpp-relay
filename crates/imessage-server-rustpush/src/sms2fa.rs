//! Read Apple's SMS 2FA code off the handset over adb - no human in the loop.
//!
//! The Apple ID's SIM lives in a handset attached to this host (serial
//! <adb-serial> by default). The aka-oracle app (com.openbubbles.aka,
//! debuggable) appends every inbound text to its private files/sms.txt:
//!
//!   [<iso>] src=SMS_RECEIVED from=<addr> time=<millis> body=<body>
//!
//! which run-as can read without root. This module fingerprints the file
//! *before* the login asks Apple for anything, then polls for the first line
//! that was not already there and carries a 6-digit code. Freshness is decided
//! by set-difference, not by comparing clocks, so a stale code from an earlier
//! attempt (or a device clock that disagrees with the host) can never be used.
//!
//! Content that comes off the handset is untrusted input: it is only ever
//! pattern-matched for a 6-digit code, never executed or interpolated into a
//! shell.

use std::collections::HashSet;
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime};

/// Handset holding the Apple ID's SIM. Override with IMESSAGE_ADB_SERIAL.
pub const DEFAULT_ADB_SERIAL: &str = "0123456789ABCDEF";
/// aka-oracle: mirrors inbound SMS into its private files/ directory.
const SMS_PACKAGE: &str = "com.openbubbles.aka";
const SMS_FILE: &str = "files/sms.txt";
/// Hard cap on one adb call so an offline handset can never wedge the login thread.
const ADB_TIMEOUT: &str = "10";

/// A verification code read off the handset.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SmsCode {
    pub code: String,
    pub from: String,
    /// The full message body, kept only so the caller can log/inspect what it
    /// matched. Never executed or interpolated into a shell.
    pub body: String,
    /// Milliseconds since the epoch, as recorded by the phone.
    pub timestamp_ms: Option<u64>,
}

impl SmsCode {
    /// Apple's SMS 2FA body says "Apple"; its sender is a short code. Anything
    /// not matching this is still acceptable when the baseline is known good,
    /// but is preferred only after an Apple-looking message.
    fn looks_like_apple(&self) -> bool {
        self.from.to_ascii_lowercase().contains("apple")
            || self.code_body_is_apple()
    }

    fn code_body_is_apple(&self) -> bool {
        self.body.to_ascii_lowercase().contains("apple")
    }
}

/// Snapshots files/sms.txt and yields the first *new* 6-digit code afterwards.
#[derive(Debug)]
pub struct SmsWatcher {
    serial: String,
    baseline: HashSet<String>,
    baseline_ok: bool,
}

impl SmsWatcher {
    /// Serial from IMESSAGE_ADB_SERIAL, else DEFAULT_ADB_SERIAL; then snapshots.
    pub fn from_env() -> SmsWatcher {
        let serial = std::env::var("IMESSAGE_ADB_SERIAL")
            .ok()
            .map(|raw| raw.trim().to_string())
            .filter(|raw| !raw.is_empty())
            .unwrap_or_else(|| DEFAULT_ADB_SERIAL.to_string());
        SmsWatcher::new(serial)
    }

    /// Snapshot the handset's SMS file now. Call this *before* asking Apple to
    /// send a code: rustpush's login loop requests the SMS before it invokes
    /// the 2FA callback, so a snapshot taken inside the callback would already
    /// contain the code and look stale.
    pub fn new(serial: String) -> SmsWatcher {
        let mut baseline = HashSet::new();
        let mut baseline_ok = false;
        for attempt in 0..3 {
            if let Some(text) = fetch(&serial) {
                baseline = text.lines().map(str::to_string).collect();
                baseline_ok = true;
                break;
            }
            if attempt < 2 {
                std::thread::sleep(Duration::from_secs(2));
            }
        }
        SmsWatcher { serial, baseline, baseline_ok }
    }

    pub fn serial(&self) -> &str {
        &self.serial
    }

    /// One poll: fetch the handset's mirror now and return a freshly-seen
    /// 6-digit code, if there is one. `wait` is a poll loop around this; the
    /// relay's 2FA wait calls it between checks of the operator-supplied code.
    pub fn poll_once(&self) -> Option<SmsCode> {
        fetch(&self.serial).and_then(|text| self.pick(&text))
    }

    /// Poll until a new inbound text with a 6-digit code appears, or the
    /// deadline passes. Returns None on timeout.
    ///
    /// The relay's 2FA wait uses `poll_once` directly (it interleaves checks of
    /// the operator-supplied code); this is kept for callers that just block.
    #[allow(dead_code)]
    pub fn wait(&self, timeout: Duration) -> Option<SmsCode> {
        let deadline = SystemTime::now() + timeout;
        loop {
            if let Some(code) = self.poll_once() {
                return Some(code);
            }
            if SystemTime::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_secs(5));
        }
    }

    /// New lines only; prefer an Apple-looking body, else the newest.
    fn pick(&self, text: &str) -> Option<SmsCode> {
        let mut fallback: Option<SmsCode> = None;
        for line in text.lines() {
            if self.baseline.contains(line) {
                continue;
            }
            let Some(code) = parse_line(line) else {
                continue;
            };
            if code.looks_like_apple() {
                return Some(code);
            }
            // If the pre-login snapshot failed we cannot tell stale from fresh,
            // so only accept an Apple-looking message rather than a random code.
            if !self.baseline_ok {
                continue;
            }
            let newer = match (code.timestamp_ms, fallback.as_ref().and_then(|c| c.timestamp_ms)) {
                (Some(a), Some(b)) => a >= b,
                (Some(_), None) => true,
                _ => fallback.is_none(),
            };
            if newer {
                fallback = Some(code);
            }
        }
        fallback
    }
}

fn fetch(serial: &str) -> Option<String> {
    // Primary: the aka-oracle mirror of the SMS_RECEIVED broadcast.
    if let Some(text) = adb(serial, &["shell", "run-as", SMS_PACKAGE, "cat", SMS_FILE]) {
        if !text.trim().is_empty() {
            return Some(text);
        }
    }
    // Fallback: the provider's own inbox. Empty on this handset today (the
    // broadcast receiver is what captures texts), kept for other devices.
    adb(
        serial,
        &[
            "shell",
            "content",
            "query",
            "--uri",
            "content://sms/inbox",
            "--projection",
            "address:body:date",
        ],
    )
}

fn adb(serial: &str, args: &[&str]) -> Option<String> {
    let output = Command::new("timeout")
        .arg(ADB_TIMEOUT)
        .arg("adb")
        .arg("-s")
        .arg(serial)
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// One files/sms.txt line (or one content query row) -> a code, if it is an
/// inbound *text* carrying a standalone 6-digit number.
fn parse_line(line: &str) -> Option<SmsCode> {
    let line = line.trim();
    if line.is_empty() || line.starts_with("[pdu]") {
        // DATA_SMS / carrier REG-RESP lines are not user-visible texts.
        return None;
    }
    if let Some(src) = field(line, "src=") {
        if src != "SMS_RECEIVED" && src != "INBOX" {
            return None;
        }
    }
    let body = {
        let idx = line.find("body=")?;
        line[idx + "body=".len()..].to_string()
    };
    let code = six_digit_code(&body)?;
    let from = field(line, "from=")
        .or_else(|| field(line, "address="))
        .unwrap_or_default()
        .to_string();
    let timestamp_ms = field(line, "time=")
        .or_else(|| field(line, "date="))
        .and_then(|value| value.parse::<u64>().ok());
    Some(SmsCode { code, from, body, timestamp_ms })
}

/// Value of key up to the next whitespace (the format is space-separated;
/// body= is always last, so bodies may contain spaces).
fn field<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    let idx = line.find(key)?;
    line[idx + key.len()..].split_whitespace().next()
}

/// The first maximal run of digits that is exactly six long. A seven-digit run
/// (a phone number) is not a code; neither is the 6 digits inside a longer
/// digit run, because the run is taken maximally.
pub fn six_digit_code(body: &str) -> Option<String> {
    let chars: Vec<char> = body.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i].is_ascii_digit() {
            let start = i;
            while i < chars.len() && chars[i].is_ascii_digit() {
                i += 1;
            }
            if i - start == 6 {
                return Some(chars[start..i].iter().collect());
            }
        } else {
            i += 1;
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(from: &str, time: u64, body: &str) -> String {
        format!("[2026-10-13T07:00:00.000-0400] src=SMS_RECEIVED from={from} time={time} body={body}")
    }

    fn watcher(baseline: &str) -> SmsWatcher {
        SmsWatcher {
            serial: "test".to_string(),
            baseline: baseline.lines().map(str::to_string).collect(),
            baseline_ok: true,
        }
    }

    #[test]
    fn extracts_a_standalone_six_digit_code() {
        assert_eq!(six_digit_code("Your Apple ID Code is: 123456."), Some("123456".to_string()));
        assert_eq!(six_digit_code("no code here"), None);
        assert_eq!(six_digit_code("call 2125551234 now"), None);
        assert_eq!(six_digit_code("id 12345"), None);
        assert_eq!(six_digit_code("id 1234567"), None);
    }

    #[test]
    fn parses_sms_received_lines() {
        let code = parse_line(&line("+15551234567", 1_791_889_000_000, "Your Apple ID Code is: 654321."))
            .expect("code");
        assert_eq!(code.code, "654321");
        assert_eq!(code.from, "+15551234567");
        assert_eq!(code.timestamp_ms, Some(1_791_889_000_000));
    }

    #[test]
    fn ignores_data_sms_and_non_text_sources() {
        assert!(parse_line("[pdu] src=DATA_SMS port=5497 body=REG-RESP?v=3;r=1;n=+1;s=AA").is_none());
        assert!(parse_line("[2026] src=SMS_TEST from=+1 time=1 body=123456").is_none());
    }

    #[test]
    fn only_new_lines_count() {
        let old = line("+1", 100, "Your Apple ID Code is: 111111.");
        let new = line("+1", 200, "Your Apple ID Code is: 222222.");
        let w = watcher(&old);
        let pick = w.pick(&format!("{old}\n{new}\n")).expect("new code");
        assert_eq!(pick.code, "222222");
        assert!(w.pick(&format!("{old}\n")).is_none());
    }

    #[test]
    fn prefers_apple_body_over_another_sender() {
        let old = line("+1", 100, "hello");
        let other = line("3538", 200, "Your Messenger verification code is 333333");
        let apple = line("+1", 300, "Your Apple ID Code is: 444444.");
        let w = watcher(&old);
        let pick = w.pick(&format!("{old}\n{other}\n{apple}\n")).expect("apple code");
        assert_eq!(pick.code, "444444");
    }

    /// End-to-end against the real handset: appends a synthetic SMS to the
    /// app's mirror over adb, then proves wait() picks up only that new line.
    /// Off by default because it writes test data into the app's files/ dir:
    ///   SMS2FA_LIVE=1 cargo test live_reads_a_new_code -- --nocapture
    #[test]
    fn live_reads_a_new_code_off_the_handset() {
        if std::env::var("SMS2FA_LIVE").ok().as_deref() != Some("1") {
            return;
        }
        let watcher = SmsWatcher::from_env();
        let millis = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("clock")
            .as_millis();
        let body = format!("Your Apple ID Code is: 654321. sms2fa live test {millis}");
        let line =
            format!("[{millis}] src=SMS_RECEIVED from=+15550001111 time={millis} body={body}");
        assert!(!line.contains('\''), "the test line must not break the remote quoting");
        // One single argument to adb shell, so the device shell sees the
        // redirection intact (same shape as SmsWatcher's own adb calls).
        let remote = format!("run-as {SMS_PACKAGE} sh -c 'echo \"{line}\" >> {SMS_FILE}'");
        assert!(
            adb(&watcher.serial(), &["shell", &remote]).is_some(),
            "could not append the synthetic SMS over adb"
        );
        let found = watcher
            .wait(Duration::from_secs(20))
            .expect("the new code should be read off the handset");
        assert_eq!(found.code, "654321");
        assert_eq!(found.from, "+15550001111");
    }
}
