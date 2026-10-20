//! On-disk storage for inbound attachments.
//!
//! Everything a sender controls is untrusted here: the file name, the declared
//! mime type, the size and the bytes. So the stored object is addressed only by
//! a random id the server mints, the name is sanitised before it is ever shown
//! or put in a header, the mime type is sniffed from the bytes, the size is
//! capped while writing, and the whole directory is bounded by a byte quota
//! and a maximum age.
//!
//! Layout inside the attachment directory, per attachment `<id>` (32 hex):
//!
//! * `<id>.bin`  - the decrypted bytes
//! * `<id>.json` - `{name, mime, size, created}` (sanitised, server-derived)
//!
//! Nothing here talks to Apple; the rustpush backend feeds it a `Write`.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};

/// Longest stored/served file name, extension included.
const MAX_NAME_LEN: usize = 100;
/// Name used when sanitising leaves nothing.
pub const FALLBACK_NAME: &str = "attachment";
/// Mime type for anything unknown or unsafe to label precisely.
pub const OCTET_STREAM: &str = "application/octet-stream";

/// Sidecar metadata, derived by the server (never copied verbatim from the wire).
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Meta {
    pub name: String,
    pub mime: String,
    pub size: u64,
    /// Unix seconds.
    pub created: i64,
}

/// Limits for one attachment directory.
#[derive(Clone, Debug)]
pub struct Limits {
    /// Largest single attachment accepted.
    pub max_file_bytes: u64,
    /// Total bytes kept; the oldest attachments are evicted beyond it.
    pub quota_bytes: u64,
    /// Attachments older than this many seconds are evicted.
    pub max_age_secs: i64,
}

/// A mint-able, validated attachment id: 32 lowercase hex characters. Anything
/// else is rejected before it can reach a path (this is what keeps `/dl/<id>`
/// from ever seeing `..` or `/`).
pub fn valid_id(id: &str) -> bool {
    id.len() == 32 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

pub fn new_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// Reduce a sender-supplied name to a safe display / download name: last path
/// component only (both separators), no control characters, no leading dots, a
/// conservative character set, bounded length with the extension preserved.
pub fn sanitize_name(raw: &str) -> String {
    let base = raw.rsplit(['/', '\\']).next().unwrap_or("");
    let mut out = String::with_capacity(base.len());
    for c in base.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | ' ' | '(' | ')') {
            out.push(c);
        } else if c.is_control() {
            // dropped
        } else {
            out.push('_');
        }
    }
    let trimmed = out.trim_matches(|c: char| c == '.' || c == ' ').to_string();
    if trimmed.is_empty() {
        return FALLBACK_NAME.to_string();
    }
    truncate_keep_ext(&trimmed, MAX_NAME_LEN)
}

fn truncate_keep_ext(name: &str, max: usize) -> String {
    if name.len() <= max {
        return name.to_string();
    }
    // The name is ASCII after sanitising, so byte slicing is safe.
    match name.rfind('.') {
        Some(dot) if name.len() - dot <= 16 && dot > 0 => {
            let ext = &name[dot..];
            format!("{}{}", &name[..max - ext.len()], ext)
        }
        _ => name[..max].to_string(),
    }
}

/// Mime type for `head` (the first bytes of the file). Falls back to the
/// sender's claim only when it is well-formed and not one of the types a
/// browser would execute; otherwise `application/octet-stream`.
pub fn sniff_mime(head: &[u8], claimed: &str) -> String {
    if let Some(m) = magic(head) {
        return m.to_string();
    }
    safe_claimed(claimed)
}

fn magic(h: &[u8]) -> Option<&'static str> {
    let starts = |p: &[u8]| h.starts_with(p);
    if starts(b"\x89PNG\r\n\x1a\n") {
        return Some("image/png");
    }
    if starts(&[0xff, 0xd8, 0xff]) {
        return Some("image/jpeg");
    }
    if starts(b"GIF87a") || starts(b"GIF89a") {
        return Some("image/gif");
    }
    if h.len() >= 12 && &h[0..4] == b"RIFF" {
        match &h[8..12] {
            b"WEBP" => return Some("image/webp"),
            b"WAVE" => return Some("audio/wav"),
            _ => {}
        }
    }
    if starts(b"%PDF-") {
        return Some("application/pdf");
    }
    if starts(b"OggS") {
        return Some("audio/ogg");
    }
    if starts(b"ID3") || (h.len() >= 2 && h[0] == 0xff && (h[1] & 0xe0) == 0xe0) {
        return Some("audio/mpeg");
    }
    if starts(b"caff") {
        return Some("audio/x-caf");
    }
    if starts(b"PK\x03\x04") {
        return Some("application/zip");
    }
    if h.len() >= 12 && &h[4..8] == b"ftyp" {
        return Some(match &h[8..12] {
            b"heic" | b"heix" | b"hevc" | b"hevx" | b"mif1" | b"msf1" => "image/heic",
            b"qt  " => "video/quicktime",
            b"M4A " | b"M4B " => "audio/mp4",
            _ => "video/mp4",
        });
    }
    None
}

fn safe_claimed(claimed: &str) -> String {
    let c = claimed.trim().to_ascii_lowercase();
    let well_formed = c.len() <= 100
        && c.split_once('/').is_some_and(|(t, s)| {
            !t.is_empty()
                && !s.is_empty()
                && t.bytes().chain(s.bytes()).all(|b| {
                    b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'+' | b'-')
                })
        });
    let active = c.contains("html")
        || c.contains("xml")
        || c.contains("javascript")
        || c.contains("ecmascript")
        || c.contains("svg")
        || c.starts_with("text/");
    if well_formed && !active {
        c
    } else {
        OCTET_STREAM.to_string()
    }
}

/// A `Write` that refuses to take more than `limit` bytes and remembers the
/// first bytes (for sniffing). MMCS hands us the plaintext through blocking
/// `Write` calls, so this is where the size cap is enforced for real, whatever
/// size the sender declared.
pub struct CappedWriter<W: Write> {
    inner: W,
    limit: u64,
    written: u64,
    head: Vec<u8>,
}

impl<W: Write> CappedWriter<W> {
    pub fn new(inner: W, limit: u64) -> Self {
        Self { inner, limit, written: 0, head: Vec::with_capacity(32) }
    }
    pub fn written(&self) -> u64 {
        self.written
    }
    pub fn head(&self) -> &[u8] {
        &self.head
    }
}

impl<W: Write> Write for CappedWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if self.written + buf.len() as u64 > self.limit {
            return Err(io::Error::new(
                io::ErrorKind::Other,
                format!("attachment exceeds the {} byte cap", self.limit),
            ));
        }
        let n = self.inner.write(buf)?;
        if self.head.len() < 32 {
            let take = (32 - self.head.len()).min(n);
            self.head.extend_from_slice(&buf[..take]);
        }
        self.written += n as u64;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Handle on one attachment directory.
#[derive(Clone, Debug)]
pub struct Store {
    dir: PathBuf,
    pub limits: Limits,
}

/// What an in-progress download looks like on disk; call [`Pending::finish`] on
/// success. Dropping it without finishing removes the partial file.
pub struct Pending {
    dir: PathBuf,
    id: String,
    part: PathBuf,
    done: bool,
}

impl Pending {
    pub fn id(&self) -> &str {
        &self.id
    }
    pub fn part_path(&self) -> &Path {
        &self.part
    }
    /// Move the bytes into place and write the sidecar. `size` and `head` come
    /// from the [`CappedWriter`]; `name`/`claimed_mime` are the sender's claims.
    pub fn finish(mut self, name: &str, claimed_mime: &str, size: u64, head: &[u8], now: i64) -> Result<(String, Meta)> {
        let meta = Meta {
            name: sanitize_name(name),
            mime: sniff_mime(head, claimed_mime),
            size,
            created: now,
        };
        let bin = self.dir.join(format!("{}.bin", self.id));
        fs::rename(&self.part, &bin).context("moving the attachment into place")?;
        let json = self.dir.join(format!("{}.json", self.id));
        let tmp = self.dir.join(format!("{}.json.tmp", self.id));
        fs::write(&tmp, serde_json::to_vec(&meta)?)?;
        fs::rename(&tmp, &json)?;
        self.done = true;
        Ok((self.id.clone(), meta))
    }
}

impl Drop for Pending {
    fn drop(&mut self) {
        if !self.done {
            let _ = fs::remove_file(&self.part);
        }
    }
}

impl Store {
    /// Open (creating, mode 0700) the directory.
    pub fn open(dir: PathBuf, limits: Limits) -> Result<Self> {
        fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o700));
        }
        Ok(Self { dir, limits })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Start a download: a fresh id and an exclusively created `.part` file
    /// (mode 0600) to write into.
    pub fn begin(&self) -> Result<(Pending, fs::File)> {
        let id = new_id();
        let part = self.dir.join(format!("{id}.part"));
        let mut opts = fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let file = opts.open(&part).with_context(|| format!("creating {}", part.display()))?;
        Ok((Pending { dir: self.dir.clone(), id, part, done: false }, file))
    }

    /// Metadata and path of a stored attachment, or None (unknown id, bad id,
    /// half-written, evicted).
    pub fn get(&self, id: &str) -> Option<(PathBuf, Meta)> {
        if !valid_id(id) {
            return None;
        }
        let raw = fs::read(self.dir.join(format!("{id}.json"))).ok()?;
        let meta: Meta = serde_json::from_slice(&raw).ok()?;
        let bin = self.dir.join(format!("{id}.bin"));
        bin.is_file().then_some((bin, meta))
    }

    fn remove(&self, id: &str) {
        let _ = fs::remove_file(self.dir.join(format!("{id}.json")));
        let _ = fs::remove_file(self.dir.join(format!("{id}.bin")));
    }

    /// Delete what the limits say to delete; returns the removed ids. Also
    /// sweeps stale `.part` files (a crash mid-download) older than an hour.
    pub fn evict(&self, now: i64) -> Vec<String> {
        let mut entries = Vec::new();
        let mut parts = Vec::new();
        if let Ok(rd) = fs::read_dir(&self.dir) {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if let Some(id) = name.strip_suffix(".json") {
                    if valid_id(id) {
                        if let Some((_, meta)) = self.get(id) {
                            entries.push(Entry { id: id.to_string(), size: meta.size, created: meta.created });
                        } else {
                            // sidecar without bytes
                            self.remove(id);
                        }
                    }
                } else if name.ends_with(".part") || name.ends_with(".json.tmp") {
                    parts.push(e.path());
                }
            }
        }
        for p in parts {
            let old = fs::metadata(&p)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.elapsed().ok())
                .is_some_and(|age| age.as_secs() > 3600);
            if old {
                let _ = fs::remove_file(p);
            }
        }
        let doomed = plan_eviction(&entries, now, &self.limits);
        for id in &doomed {
            self.remove(id);
        }
        doomed
    }

    /// Total bytes currently stored (sum of sidecar sizes).
    pub fn total_bytes(&self) -> u64 {
        let mut total = 0;
        if let Ok(rd) = fs::read_dir(&self.dir) {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if let Some(id) = name.strip_suffix(".json") {
                    if let Some((_, m)) = self.get(id) {
                        total += m.size;
                    }
                }
            }
        }
        total
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub id: String,
    pub size: u64,
    pub created: i64,
}

/// Which ids to delete: everything past `max_age_secs`, then oldest-first until
/// the remainder fits `quota_bytes`. Pure, so it is unit tested directly.
pub fn plan_eviction(entries: &[Entry], now: i64, limits: &Limits) -> Vec<String> {
    let mut doomed: Vec<String> = Vec::new();
    let mut keep: Vec<&Entry> = Vec::new();
    for e in entries {
        if now - e.created > limits.max_age_secs {
            doomed.push(e.id.clone());
        } else {
            keep.push(e);
        }
    }
    keep.sort_by_key(|e| e.created);
    let mut total: u64 = keep.iter().map(|e| e.size).sum();
    for e in keep {
        if total <= limits.quota_bytes {
            break;
        }
        total -= e.size;
        doomed.push(e.id.clone());
    }
    doomed
}

/// Convenience used by the backend: download into a fresh slot. `fetch` receives
/// a capped writer over the `.part` file and performs the MMCS read.
pub async fn store_with<F, Fut>(
    store: &Store,
    name: &str,
    claimed_mime: &str,
    declared_size: u64,
    now: i64,
    fetch: F,
) -> Result<(String, Meta)>
where
    F: FnOnce(CappedWriter<fs::File>) -> Fut,
    Fut: std::future::Future<Output = Result<CappedWriter<fs::File>>>,
{
    if declared_size > store.limits.max_file_bytes {
        return Err(anyhow!(
            "attachment declares {declared_size} bytes, over the {} byte cap",
            store.limits.max_file_bytes
        ));
    }
    let (pending, file) = store.begin()?;
    let writer = CappedWriter::new(file, store.limits.max_file_bytes);
    let writer = fetch(writer).await?;
    let (written, head) = (writer.written(), writer.head().to_vec());
    drop(writer);
    let out = pending.finish(name, claimed_mime, written, &head, now)?;
    store.evict(now);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("attach-{tag}-{}", uuid::Uuid::new_v4()))
    }

    fn limits() -> Limits {
        Limits { max_file_bytes: 1024, quota_bytes: 100, max_age_secs: 1000 }
    }

    #[test]
    fn names_are_sanitised() {
        assert_eq!(sanitize_name("photo.jpg"), "photo.jpg");
        assert_eq!(sanitize_name("../../etc/passwd"), "passwd");
        assert_eq!(sanitize_name("C:\\Users\\x\\evil.exe"), "evil.exe");
        assert_eq!(sanitize_name("/abs/dir/"), FALLBACK_NAME);
        assert_eq!(sanitize_name("..."), FALLBACK_NAME);
        assert_eq!(sanitize_name(".hidden"), "hidden");
        assert_eq!(sanitize_name("a\r\nb\0c.txt"), "abc.txt");
        assert_eq!(sanitize_name("na\"me;<x>.png"), "na_me__x_.png");
        assert_eq!(sanitize_name("日本語.png"), "___.png");
        assert_eq!(sanitize_name(""), FALLBACK_NAME);
        let long = format!("{}.jpeg", "a".repeat(300));
        let s = sanitize_name(&long);
        assert_eq!(s.len(), MAX_NAME_LEN);
        assert!(s.ends_with(".jpeg"));
    }

    #[test]
    fn mime_is_sniffed_not_trusted() {
        assert_eq!(sniff_mime(b"\x89PNG\r\n\x1a\nxxxx", "text/html"), "image/png");
        assert_eq!(sniff_mime(&[0xff, 0xd8, 0xff, 0xe0], ""), "image/jpeg");
        assert_eq!(sniff_mime(b"RIFF\0\0\0\0WEBPVP8 ", ""), "image/webp");
        assert_eq!(sniff_mime(b"\0\0\0\x18ftypheic....", ""), "image/heic");
        assert_eq!(sniff_mime(b"\0\0\0\x14ftypqt  ....", ""), "video/quicktime");
        assert_eq!(sniff_mime(b"\0\0\0\x18ftypmp42....", ""), "video/mp4");
        assert_eq!(sniff_mime(b"%PDF-1.7", "image/png"), "application/pdf");
        // Unknown bytes: only a harmless, well-formed claim survives.
        assert_eq!(sniff_mime(b"????", "audio/amr"), "audio/amr");
        assert_eq!(sniff_mime(b"<html>", "text/html"), OCTET_STREAM);
        assert_eq!(sniff_mime(b"<svg", "image/svg+xml"), OCTET_STREAM);
        assert_eq!(sniff_mime(b"x", "application/javascript"), OCTET_STREAM);
        assert_eq!(sniff_mime(b"x", "not a mime"), OCTET_STREAM);
        assert_eq!(sniff_mime(b"x", "IMAGE/X-Foo"), "image/x-foo");
    }

    #[test]
    fn ids_are_validated() {
        assert!(valid_id(&new_id()));
        assert!(!valid_id(""));
        assert!(!valid_id("../etc/passwd"));
        assert!(!valid_id(&"A".repeat(32)));
        assert!(!valid_id(&"a".repeat(31)));
        assert!(!valid_id(&format!("{}/", "a".repeat(31))));
    }

    #[test]
    fn capped_writer_stops_at_the_limit_and_keeps_the_head() {
        let mut w = CappedWriter::new(Vec::new(), 10);
        w.write_all(b"12345").unwrap();
        assert_eq!(w.head(), b"12345");
        assert!(w.write_all(b"678901").is_err());
        assert_eq!(w.written(), 5);
        w.write_all(b"67890").unwrap();
        assert_eq!(w.written(), 10);
    }

    #[test]
    fn eviction_plan_applies_age_then_quota() {
        let e = |id: &str, size, created| Entry { id: id.into(), size, created };
        let l = Limits { max_file_bytes: 0, quota_bytes: 100, max_age_secs: 1000 };
        // old one goes by age; of the rest (60+50=110>100) the oldest goes.
        let got = plan_eviction(&[e("old", 1, 0), e("a", 60, 1500), e("b", 50, 1600)], 2000, &l);
        assert_eq!(got, vec!["old".to_string(), "a".to_string()]);
        // within limits: nothing.
        assert!(plan_eviction(&[e("a", 60, 1500), e("b", 40, 1600)], 2000, &l).is_empty());
        assert!(plan_eviction(&[], 2000, &l).is_empty());
    }

    #[test]
    fn store_round_trip_and_eviction() {
        let dir = tmp("rt");
        let store = Store::open(dir.clone(), limits()).unwrap();
        let (pending, mut f) = store.begin().unwrap();
        f.write_all(b"\x89PNG\r\n\x1a\nbody").unwrap();
        drop(f);
        let (id, meta) = pending
            .finish("../../x.png", "text/html", 12, b"\x89PNG\r\n\x1a\nbody", 100)
            .unwrap();
        assert_eq!(meta.name, "x.png");
        assert_eq!(meta.mime, "image/png");
        let (path, got) = store.get(&id).unwrap();
        assert_eq!(got, meta);
        assert_eq!(fs::read(path).unwrap(), b"\x89PNG\r\n\x1a\nbody");
        assert_eq!(store.total_bytes(), 12);
        assert!(store.get("../x").is_none());

        // Age eviction removes both files.
        assert!(store.evict(100).is_empty());
        assert_eq!(store.evict(100 + 1001), vec![id.clone()]);
        assert!(store.get(&id).is_none());
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn dropped_pending_leaves_nothing() {
        let dir = tmp("drop");
        let store = Store::open(dir.clone(), limits()).unwrap();
        let (pending, mut f) = store.begin().unwrap();
        f.write_all(b"partial").unwrap();
        drop(f);
        drop(pending);
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn store_with_enforces_declared_and_actual_size() {
        let dir = tmp("cap");
        let store = Store::open(dir.clone(), limits()).unwrap();
        // Declared too big: never touches the disk.
        let r = store_with(&store, "a", "", 5000, 1, |w| async move { Ok(w) }).await;
        assert!(r.is_err());
        // Declared small but the stream is bigger: the capped writer trips.
        let r = store_with(&store, "a", "", 10, 1, |mut w| async move {
            w.write_all(&vec![0u8; 2000])?;
            Ok(w)
        })
        .await;
        assert!(r.is_err());
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 0, "partial file removed");
        // Honest small file works.
        let (id, meta) = store_with(&store, "n.bin", "audio/amr", 3, 5, |mut w| async move {
            w.write_all(b"abc")?;
            Ok(w)
        })
        .await
        .unwrap();
        assert_eq!((meta.size, meta.mime.as_str()), (3, "audio/amr"));
        assert!(store.get(&id).is_some());
        let _ = fs::remove_dir_all(&dir);
    }
}
