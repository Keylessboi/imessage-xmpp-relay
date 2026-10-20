//! The public attachment download listener.
//!
//! This is a SEPARATE listener from the control API (`api.rs`): it serves
//! exactly one route, `GET /dl/<id>?exp=<unix>&sig=<hex>`, and nothing else, so
//! publishing this port through an Ingress never exposes `/im/*`. The signature
//! is the only gate: HMAC-SHA256 over `<id>\n<exp>` with the key from
//! `IMESSAGE_FILE_HMAC_KEY_FILE`, compared in constant time, short-lived.
//!
//! Files are untrusted third-party content, so every response is a forced
//! download (`Content-Disposition: attachment`, `nosniff`, a sandboxing CSP,
//! `no-store`) with a server-sniffed content type and a sanitised file name.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::io::AsyncReadExt;
use tokio_stream::wrappers::ReceiverStream;

use crate::attach::{valid_id, Store};
use crate::hexutil::{decode_hex, encode_hex};

pub fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// HMAC-SHA256 (RFC 2104), hand-rolled over `sha2` so the server needs no new
/// dependency. Checked against the RFC 4231 vectors in the tests.
pub fn hmac_sha256(key: &[u8], msg: &[u8]) -> [u8; 32] {
    const BLOCK: usize = 64;
    let mut k = [0u8; BLOCK];
    if key.len() > BLOCK {
        k[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(k.map(|b| b ^ 0x36));
    inner.update(msg);
    let inner = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(k.map(|b| b ^ 0x5c));
    outer.update(inner);
    outer.finalize().into()
}

/// Constant-time equality for equal-length secrets (length is not secret).
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

#[derive(Clone)]
pub struct Signer {
    key: Vec<u8>,
}

impl std::fmt::Debug for Signer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Signer(<redacted>)")
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Denied {
    BadSignature,
    Expired,
}

impl Signer {
    pub fn new(key: Vec<u8>) -> Self {
        Self { key }
    }

    fn mac(&self, id: &str, exp: i64) -> [u8; 32] {
        hmac_sha256(&self.key, format!("{id}\n{exp}").as_bytes())
    }

    pub fn sign(&self, id: &str, exp: i64) -> String {
        encode_hex(&self.mac(id, exp))
    }

    /// `/dl/<id>?exp=..&sig=..`, valid for `ttl_secs` from `now`.
    pub fn signed_path(&self, id: &str, now: i64, ttl_secs: i64) -> String {
        let exp = now + ttl_secs;
        format!("/dl/{id}?exp={exp}&sig={}", self.sign(id, exp))
    }

    /// Signature is checked BEFORE expiry so an attacker learns nothing about
    /// which ids exist or when they lapse.
    pub fn verify(&self, id: &str, exp: i64, sig_hex: &str, now: i64) -> Result<(), Denied> {
        let given = decode_hex(sig_hex).ok_or(Denied::BadSignature)?;
        if !ct_eq(&given, &self.mac(id, exp)) {
            return Err(Denied::BadSignature);
        }
        if now > exp {
            return Err(Denied::Expired);
        }
        Ok(())
    }
}

#[derive(Clone)]
pub struct FilesState {
    pub store: Store,
    pub signer: Signer,
}

#[derive(Deserialize)]
struct DlQuery {
    exp: i64,
    sig: String,
}

fn status(code: StatusCode) -> Response {
    (code, [(header::CACHE_CONTROL, "no-store")]).into_response()
}

async fn download(
    State(st): State<Arc<FilesState>>,
    Path(id): Path<String>,
    q: Result<Query<DlQuery>, axum::extract::rejection::QueryRejection>,
) -> Response {
    let Ok(Query(q)) = q else {
        return status(StatusCode::FORBIDDEN);
    };
    if !valid_id(&id) {
        return status(StatusCode::FORBIDDEN);
    }
    if st.signer.verify(&id, q.exp, &q.sig, unix_now()).is_err() {
        return status(StatusCode::FORBIDDEN);
    }
    let Some((path, meta)) = st.store.get(&id) else {
        return status(StatusCode::NOT_FOUND);
    };
    let Ok(mut file) = tokio::fs::File::open(&path).await else {
        return status(StatusCode::NOT_FOUND);
    };
    let Ok(len) = file.metadata().await.map(|m| m.len()) else {
        return status(StatusCode::NOT_FOUND);
    };

    let (tx, rx) = tokio::sync::mpsc::channel::<std::io::Result<Bytes>>(4);
    tokio::spawn(async move {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            match file.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    if tx.send(Ok(Bytes::copy_from_slice(&buf[..n]))).await.is_err() {
                        break;
                    }
                }
                Err(e) => {
                    let _ = tx.send(Err(e)).await;
                    break;
                }
            }
        }
    });

    let mut resp = Response::new(Body::from_stream(ReceiverStream::new(rx)));
    let h = resp.headers_mut();
    let hv = |s: &str| HeaderValue::from_str(s).unwrap_or_else(|_| HeaderValue::from_static(""));
    h.insert(header::CONTENT_TYPE, hv(&meta.mime));
    h.insert(header::CONTENT_LENGTH, hv(&len.to_string()));
    // `meta.name` is sanitised to [A-Za-z0-9._ ()-], so quoting is safe.
    h.insert(
        header::CONTENT_DISPOSITION,
        hv(&format!("attachment; filename=\"{}\"", meta.name)),
    );
    h.insert(header::X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("private, no-store"));
    h.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static("default-src 'none'; sandbox"),
    );
    h.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    resp
}

/// The only router on the public port. Anything but `GET /dl/<id>` is a bare 404.
pub fn router(state: Arc<FilesState>) -> Router {
    Router::new()
        .route("/dl/:id", get(download))
        .fallback(|| async { StatusCode::NOT_FOUND })
        .with_state(state)
}

/// Serve the download listener and run the eviction sweep hourly.
pub async fn serve(state: Arc<FilesState>, listen: std::net::SocketAddr) -> anyhow::Result<()> {
    let sweeper = state.store.clone();
    tokio::spawn(async move {
        loop {
            let removed = sweeper.evict(unix_now());
            if !removed.is_empty() {
                tracing::info!(count = removed.len(), "evicted old attachments");
            }
            tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
        }
    });
    let listener = tokio::net::TcpListener::bind(listen).await?;
    tracing::info!("attachment downloads on http://{listen} (GET /dl/<id> only)");
    axum::serve(listener, router(state)).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::attach::Limits;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    #[test]
    fn hmac_matches_rfc4231() {
        // Test case 1
        assert_eq!(
            encode_hex(&hmac_sha256(&[0x0b; 20], b"Hi There")),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
        // Test case 2
        assert_eq!(
            encode_hex(&hmac_sha256(b"Jefe", b"what do ya want for nothing?")),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
        // Test case 6: key longer than the block size
        assert_eq!(
            encode_hex(&hmac_sha256(
                &[0xaa; 131],
                b"Test Using Larger Than Block-Size Key - Hash Key First"
            )),
            "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"
        );
    }

    #[test]
    fn constant_time_compare() {
        assert!(ct_eq(b"abc", b"abc"));
        assert!(!ct_eq(b"abc", b"abd"));
        assert!(!ct_eq(b"abc", b"ab"));
    }

    #[test]
    fn signatures_bind_id_and_expiry() {
        let s = Signer::new(b"0123456789abcdef".to_vec());
        let sig = s.sign("id1", 1000);
        assert_eq!(s.verify("id1", 1000, &sig, 999), Ok(()));
        assert_eq!(s.verify("id1", 1000, &sig, 1000), Ok(()));
        assert_eq!(s.verify("id1", 1000, &sig, 1001), Err(Denied::Expired));
        // Tampered id, exp, sig, garbage.
        assert_eq!(s.verify("id2", 1000, &sig, 0), Err(Denied::BadSignature));
        assert_eq!(s.verify("id1", 2000, &sig, 0), Err(Denied::BadSignature));
        assert_eq!(s.verify("id1", 1000, "00", 0), Err(Denied::BadSignature));
        assert_eq!(s.verify("id1", 1000, "zz", 0), Err(Denied::BadSignature));
        // A different key does not verify.
        let other = Signer::new(b"fedcba9876543210".to_vec());
        assert_eq!(other.verify("id1", 1000, &sig, 0), Err(Denied::BadSignature));
        assert!(s.signed_path("id1", 10, 60).starts_with("/dl/id1?exp=70&sig="));
    }

    fn setup() -> (Arc<FilesState>, String, std::path::PathBuf) {
        let dir = std::env::temp_dir().join(format!("files-{}", uuid::Uuid::new_v4()));
        let store = Store::open(
            dir.clone(),
            Limits { max_file_bytes: 1 << 20, quota_bytes: 1 << 20, max_age_secs: 1 << 30 },
        )
        .unwrap();
        let (pending, mut f) = store.begin().unwrap();
        use std::io::Write;
        f.write_all(b"<html>hello</html>").unwrap();
        drop(f);
        let (id, _) = pending
            .finish("../evil\".html", "text/html", 18, b"<html>hello</html>", unix_now())
            .unwrap();
        let st = Arc::new(FilesState { store, signer: Signer::new(b"0123456789abcdef".to_vec()) });
        (st, id, dir)
    }

    async fn get_status(st: &Arc<FilesState>, uri: &str) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let resp = router(st.clone())
            .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
            .await
            .unwrap();
        let (parts, body) = resp.into_parts();
        (parts.status, parts.headers, body.collect().await.unwrap().to_bytes().to_vec())
    }

    #[tokio::test]
    async fn serves_a_signed_download_as_an_attachment() {
        let (st, id, dir) = setup();
        let path = st.signer.signed_path(&id, unix_now(), 60);
        let (code, h, body) = get_status(&st, &path).await;
        assert_eq!(code, StatusCode::OK);
        assert_eq!(body, b"<html>hello</html>");
        // Sniffing found no magic; the claimed text/html is refused.
        assert_eq!(h[header::CONTENT_TYPE], "application/octet-stream");
        assert_eq!(h[header::CONTENT_DISPOSITION], "attachment; filename=\"evil_.html\"");
        assert_eq!(h[header::X_CONTENT_TYPE_OPTIONS], "nosniff");
        assert_eq!(h[header::CONTENT_LENGTH], "18");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn rejects_bad_signature_expiry_unknown_and_other_routes() {
        let (st, id, dir) = setup();
        let now = unix_now();
        let good = st.signer.signed_path(&id, now, 60);
        // forged signature
        let forged = format!("/dl/{id}?exp={}&sig={}", now + 60, "0".repeat(64));
        assert_eq!(get_status(&st, &forged).await.0, StatusCode::FORBIDDEN);
        // expired (validly signed in the past)
        let old = st.signer.signed_path(&id, now - 1000, 60);
        assert_eq!(get_status(&st, &old).await.0, StatusCode::FORBIDDEN);
        // missing query
        assert_eq!(get_status(&st, &format!("/dl/{id}")).await.0, StatusCode::FORBIDDEN);
        // validly signed but no such file -> 404 only after the signature passed
        let ghost = "a".repeat(32);
        let p = st.signer.signed_path(&ghost, now, 60);
        assert_eq!(get_status(&st, &p).await.0, StatusCode::NOT_FOUND);
        // path traversal attempt in the id
        let sig = st.signer.sign("..", now + 60);
        assert_eq!(
            get_status(&st, &format!("/dl/..?exp={}&sig={sig}", now + 60)).await.0,
            StatusCode::FORBIDDEN
        );
        // nothing else is routed: no listing, no API, no POST
        for uri in ["/", "/dl", "/dl/", "/im/status", "/im/events", "/health"] {
            assert_eq!(get_status(&st, uri).await.0, StatusCode::NOT_FOUND, "{uri}");
        }
        let post = router(st.clone())
            .oneshot(Request::builder().method("POST").uri(&good).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(post.status(), StatusCode::METHOD_NOT_ALLOWED);
        let _ = std::fs::remove_dir_all(dir);
    }
}
