//! Config directory loading.
//!
//! Default directory is `/data` (override with `IMESSAGE_DATA_DIR`). It must hold:
//!
//! * `hwconfig.plist` - a serialised rustpush `MacOSConfig` (Mac hardware
//!   identity). Alternatives: `IMESSAGE_HWCONFIG_PLIST_BASE64` (base64 plist).
//! * `gsa.plist` - `{ user: string, pass: <data: sha256(password)> }`.
//!   Alternatives: `IMESSAGE_APPLE_ID` + `IMESSAGE_APPLE_PASSWORD` (plaintext)
//!   or `IMESSAGE_APPLE_PASSWORD_SHA256` (hex).
//! * `.env` is loaded from the current directory and the data directory.
//!
//! Nothing in here is ever logged: only presence and shape.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use base64::Engine;
use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::hexutil;

pub const DEFAULT_DATA_DIR: &str = "/data";
pub const DEFAULT_LISTEN: &str = "127.0.0.1:8790";

/// Apple ID credentials. `pass_sha256` is the SHA-256 of the password, which is
/// what rustpush/GSR expects (see `imessage-lab/rustpush/src/test.rs`).
#[derive(Clone)]
pub struct GsaConfig {
    pub user: String,
    pub pass_sha256: Vec<u8>,
}

impl std::fmt::Debug for GsaConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never let the account material reach a log line via Debug.
        f.debug_struct("GsaConfig")
            .field("user", &self.user)
            .field("pass_sha256", &"<redacted>")
            .finish()
    }
}

pub struct Config {
    pub data_dir: PathBuf,
    pub listen: SocketAddr,
    pub gsa: GsaConfig,
    /// Raw bytes of the hardware config plist, so the real backend can deserialize
    /// it straight into `MacOSConfig`.
    pub hwconfig_raw: Vec<u8>,
    /// Where the hardware config came from (path or env var name), for logs.
    pub hwconfig_source: String,
}

impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("data_dir", &self.data_dir)
            .field("listen", &self.listen)
            .field("gsa", &self.gsa)
            .field("hwconfig_source", &self.hwconfig_source)
            .field(
                "hwconfig_raw",
                &format!("<{} bytes>", self.hwconfig_raw.len()),
            )
            .finish()
    }
}

#[derive(Deserialize)]
struct GsaPlist {
    user: String,
    pass: plist::Data,
}

fn env_opt(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty())
}

/// Load `.env` from the cwd and from the data directory (if it exists). Both are
/// optional; a missing file is not an error, but a parse error is logged (and
/// never echoed, since the file holds a password). Note that dotenvy rejects
/// unquoted values containing spaces, so passwords need quotes.
fn load_dotenv() {
    let mut files = vec![PathBuf::from(".env")];
    let data_dir = env_opt("IMESSAGE_DATA_DIR").unwrap_or_else(|| DEFAULT_DATA_DIR.to_string());
    let data_env = PathBuf::from(&data_dir).join(".env");
    if data_env != Path::new(".env") {
        files.push(data_env);
    }

    for path in files {
        if !path.exists() {
            continue;
        }
        if let Err(e) = dotenvy::from_path(&path) {
            tracing::warn!(
                "{} could not be parsed ({}); variables from it are NOT set - quote values that contain spaces",
                path.display(),
                e
            );
        }
    }
}

fn load_hwconfig(data_dir: &Path) -> Result<(Vec<u8>, String)> {
    if let Some(b64) = env_opt("IMESSAGE_HWCONFIG_PLIST_BASE64") {
        let raw = base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .context("IMESSAGE_HWCONFIG_PLIST_BASE64 is not valid base64")?;
        return Ok((raw, "IMESSAGE_HWCONFIG_PLIST_BASE64".to_string()));
    }
    let path = data_dir.join("hwconfig.plist");
    if !path.exists() {
        bail!(
            "no hardware config: expected {} (or set IMESSAGE_HWCONFIG_PLIST_BASE64)",
            path.display()
        );
    }
    let raw = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
    Ok((raw, path.display().to_string()))
}

fn load_gsa(data_dir: &Path) -> Result<GsaConfig> {
    if let Some(user) = env_opt("IMESSAGE_APPLE_ID") {
        let pass_sha256 = if let Some(hex_hash) = env_opt("IMESSAGE_APPLE_PASSWORD_SHA256") {
            let bytes = hexutil::decode_hex(&hex_hash)
                .ok_or_else(|| anyhow!("IMESSAGE_APPLE_PASSWORD_SHA256 is not valid hex"))?;
            if bytes.len() != 32 {
                bail!("IMESSAGE_APPLE_PASSWORD_SHA256 must be 32 bytes (64 hex chars)");
            }
            bytes
        } else if let Some(password) = env_opt("IMESSAGE_APPLE_PASSWORD") {
            Sha256::digest(password.as_bytes()).to_vec()
        } else {
            bail!("IMESSAGE_APPLE_ID is set but neither IMESSAGE_APPLE_PASSWORD nor IMESSAGE_APPLE_PASSWORD_SHA256 is");
        };
        return Ok(GsaConfig { user, pass_sha256 });
    }

    let path = data_dir.join("gsa.plist");
    if !path.exists() {
        bail!(
            "no Apple ID: expected {} or the IMESSAGE_APPLE_ID / IMESSAGE_APPLE_PASSWORD[_SHA256] env vars",
            path.display()
        );
    }
    let raw = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
    let parsed: GsaPlist = plist::from_bytes(&raw).with_context(|| {
        format!(
            "{} must be a plist dict with 'user' and 'pass'",
            path.display()
        )
    })?;
    if parsed.user.trim().is_empty() {
        bail!("{}: 'user' is empty", path.display());
    }
    if parsed.pass.as_ref().len() != 32 {
        bail!(
            "{}: 'pass' must be the 32-byte sha256 of the password",
            path.display()
        );
    }
    Ok(GsaConfig {
        user: parsed.user,
        pass_sha256: parsed.pass.into(),
    })
}

impl Config {
    /// Read the environment, the `.env` files and the config directory.
    pub fn load() -> Result<Config> {
        load_dotenv();

        let data_dir =
            PathBuf::from(env_opt("IMESSAGE_DATA_DIR").unwrap_or_else(|| DEFAULT_DATA_DIR.into()));
        let listen_raw = env_opt("LISTEN").unwrap_or_else(|| DEFAULT_LISTEN.to_string());
        let listen: SocketAddr = listen_raw
            .parse()
            .with_context(|| format!("LISTEN={listen_raw:?} is not a host:port socket address"))?;

        let (hwconfig_raw, hwconfig_source) = load_hwconfig(&data_dir)?;
        plist::from_bytes::<plist::Value>(&hwconfig_raw)
            .with_context(|| format!("{} is not a valid plist", hwconfig_source))?;

        let gsa = load_gsa(&data_dir)?;

        Ok(Config {
            data_dir,
            listen,
            gsa,
            hwconfig_raw,
            hwconfig_source,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_listen_is_localhost() {
        assert_eq!(DEFAULT_LISTEN, "127.0.0.1:8790");
    }

    #[test]
    fn gsa_plist_shape_is_validated() {
        let dir = std::env::temp_dir().join(format!("ims-cfg-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("gsa.plist");
        let mut buf = Vec::new();
        plist::to_writer_xml(
            &mut buf,
            &plist::Value::Dictionary(plist::Dictionary::from_iter([
                (
                    "user".to_string(),
                    plist::Value::String("me@example.com".into()),
                ),
                ("pass".to_string(), plist::Value::Data(vec![0u8; 32].into())),
            ])),
        )
        .unwrap();
        std::fs::write(&path, &buf).unwrap();
        let gsa = load_gsa(&dir).unwrap();
        assert_eq!(gsa.user, "me@example.com");
        assert_eq!(gsa.pass_sha256.len(), 32);

        // Wrong hash length is rejected.
        let mut buf = Vec::new();
        plist::to_writer_xml(
            &mut buf,
            &plist::Value::Dictionary(plist::Dictionary::from_iter([
                (
                    "user".to_string(),
                    plist::Value::String("me@example.com".into()),
                ),
                ("pass".to_string(), plist::Value::Data(vec![0u8; 8].into())),
            ])),
        )
        .unwrap();
        std::fs::write(&path, &buf).unwrap();
        assert!(load_gsa(&dir).is_err());

        let _ = std::fs::remove_dir_all(&dir);
    }
}
