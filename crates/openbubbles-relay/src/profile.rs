//! Hardware profile -> validation data, via the emulated Apple NAC binary.
//!
//! Mirrors rustpush's `MacOSConfig::generate_validation_data` (macos.rs:93):
//!   bag(id-validation-cert) -> GET cert chain -> ValidationCtx::new
//!   -> POST id-initialize-validation -> key_establishment -> sign

use std::io::Read;
use std::sync::{Arc, OnceLock};

use open_absinthe::nac::{HardwareConfig, ValidationCtx};
use rustls::pki_types::CertificateDer;
use rustls::RootCertStore;
use serde::{Deserialize, Serialize};

/// Apple's own published root CAs. `Apple Server Authentication CA 2` chains to
/// Apple Root CA - G3, which is absent from the webpki-roots bundle, so a plain
/// rustls client fails with `UnknownIssuer` against init.ess.apple.com.
/// https://www.apple.com/certificateauthority/
/// Same pin set rustpush uses (rustpush/src/util.rs:254). Apple's IDS roots are
/// not in the Mozilla/webpki store at all, so they must be added explicitly.
const APPLE_ROOTS: &[&[u8]] = &[
    include_bytes!("../../../certs/AppleIncRootCertificate.cer"),
    include_bytes!("../../../certs/AppleRootCA-G2.cer"),
    include_bytes!("../../../certs/AppleRootCA-G3.cer"),
];

/// One shared agent: webpki-roots plus Apple's roots.
fn agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();

        let mut roots = RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        for der in APPLE_ROOTS {
            roots
                .add(CertificateDer::from(der.to_vec()))
                .expect("Apple root rejected by rustls");
        }

        let cfg = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();

        ureq::AgentBuilder::new()
            .tls_config(Arc::new(cfg))
            .build()
    })
}

/// Apple's IDS bag index 3.
pub const IDS_BAG: &str = "https://init.ess.apple.com/WebObjects/VCInit.woa/wa/getBag?ix=3";

/// Decode a hex string (separators tolerated) into bytes.
fn unhex(s: &str) -> Vec<u8> {
    let clean: String = s.chars().filter(|c| c.is_ascii_hexdigit()).collect();
    (0..clean.len() / 2)
        .filter_map(|i| u8::from_str_radix(&clean[i * 2..i * 2 + 2], 16).ok())
        .collect()
}

/// Byte fields are carried as hex strings so the store stays human-editable
/// and round-trips through plain JSON without custom (de)serializers.
#[derive(Clone, Serialize, Deserialize)]
pub struct HwJson {
    pub product_name: String,
    pub io_mac_address: String,
    pub platform_serial_number: String,
    pub platform_uuid: String,
    pub root_disk_uuid: String,
    pub board_id: String,
    pub os_build_num: String,
    pub platform_serial_number_enc: String,
    pub platform_uuid_enc: String,
    pub root_disk_uuid_enc: String,
    pub rom: String,
    pub rom_enc: String,
    pub mlb: String,
    pub mlb_enc: String,
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Profile {
    pub software_build_id: String,
    /// "macOS" -> one-shot Mac mode; "iPhone OS" -> continual relay mode.
    pub software_name: String,
    pub software_version: String,
    /// Machine identifier, e.g. MacBookPro16,1
    pub hardware_version: String,
    pub unique_device_id: String,
    pub icloud_ua: String,
    pub aoskit_version: String,
    pub protocol_version: u32,
    pub hw: HwJson,
}

impl Profile {
    pub fn hardware_config(&self) -> HardwareConfig {
        let mac = unhex(&self.hw.io_mac_address);
        let mut io_mac_address = [0u8; 6];
        let n = mac.len().min(6);
        io_mac_address[..n].copy_from_slice(&mac[..n]);

        HardwareConfig {
            product_name: self.hw.product_name.clone(),
            io_mac_address,
            platform_serial_number: self.hw.platform_serial_number.clone(),
            platform_uuid: self.hw.platform_uuid.clone(),
            root_disk_uuid: self.hw.root_disk_uuid.clone(),
            board_id: self.hw.board_id.clone(),
            os_build_num: self.hw.os_build_num.clone(),
            platform_serial_number_enc: unhex(&self.hw.platform_serial_number_enc),
            platform_uuid_enc: unhex(&self.hw.platform_uuid_enc),
            root_disk_uuid_enc: unhex(&self.hw.root_disk_uuid_enc),
            rom: unhex(&self.hw.rom),
            rom_enc: unhex(&self.hw.rom_enc),
            mlb: self.hw.mlb.clone(),
            mlb_enc: unhex(&self.hw.mlb_enc),
        }
    }

    /// A \`hwconfig.plist\` in rustpush's \`MacOSConfig\` shape.
    ///
    /// rustpush stores this file after the first successful registration and
    /// never needs validation data again ("Validation data will not be required
    /// for subsequent re-registrations" — rustpush/src/test.rs:291), so handing
    /// it the same hardware profile this relay signs for keeps the two halves
    /// of the identity consistent.
    ///
    /// Field names are plain snake_case: \`MacOSConfig\` carries no serde rename,
    /// and its \`*_enc\` byte fields serialize as plist \`<data>\`.
    pub fn macos_config_plist(&self) -> plist::Value {
        let mut d = plist::Dictionary::new();
        d.insert(
            "inner".into(),
            plist::to_value(&self.hardware_config()).expect("HardwareConfig is serializable"),
        );
        d.insert("version".into(), self.software_version.clone().into());
        // MacOSConfig.protocol_version is u32; plist wants an unsigned integer.
        d.insert("protocol_version".into(), (self.protocol_version as u64).into());
        d.insert("device_id".into(), self.unique_device_id.clone().into());
        d.insert("icloud_ua".into(), self.icloud_ua.clone().into());
        d.insert("aoskit_version".into(), self.aoskit_version.clone().into());
        d.insert("udid".into(), self.unique_device_id.clone().into());
        plist::Value::Dictionary(d)
    }
}

fn read_all(url: &str) -> anyhow::Result<Vec<u8>> {
    let mut buf = Vec::new();
    agent()
        .get(url)
        .call()
        .map_err(|e| anyhow::anyhow!("GET {url}: {e}"))?
        .into_reader()
        .read_to_end(&mut buf)?;
    Ok(buf)
}

/// Resolve an item name through Apple's bag.
fn bag_item(item: &str) -> anyhow::Result<String> {
    let raw = read_all(IDS_BAG)?;
    let top: plist::Value = plist::from_bytes(&raw)?;
    let inner = top
        .as_dictionary()
        .and_then(|d| d.get("bag"))
        .and_then(|v| v.as_data())
        .ok_or_else(|| anyhow::anyhow!("bag: missing 'bag' key"))?;
    let map: plist::Value = plist::from_bytes(inner)?;
    map.as_dictionary()
        .and_then(|d| d.get(item))
        .and_then(|v| v.as_string())
        .map(|s| s.to_string())
        .ok_or_else(|| anyhow::anyhow!("bag: no item {item}"))
}

/// Produce fresh validation data for this profile. Blocking; run on a thread.
pub fn acquire_validation_data(p: &Profile) -> anyhow::Result<Vec<u8>> {
    let cert_url = bag_item("id-validation-cert")?;
    let cert_raw = read_all(&cert_url)?;
    let cert_val: plist::Value = plist::from_bytes(&cert_raw)?;
    let cert = cert_val
        .as_dictionary()
        .and_then(|d| d.get("cert"))
        .and_then(|v| v.as_data())
        .map(|d| d.to_vec())
        .ok_or_else(|| anyhow::anyhow!("cert plist: missing 'cert'"))?;

    let init_url = bag_item("id-initialize-validation")?;

    let hw = p.hardware_config();
    let mut request = Vec::new();
    let mut ctx = ValidationCtx::new(&cert, &mut request, &hw)
        .map_err(|e| anyhow::anyhow!("nac_init: {e}"))?;

    let mut dict = plist::Dictionary::new();
    dict.insert("session-info-request".into(), plist::Value::Data(request));
    let mut body = Vec::new();
    plist::to_writer_xml(&mut body, &plist::Value::Dictionary(dict))?;

    let resp = agent()
        .post(&init_url)
        .set("Content-Type", "application/x-apple-plist")
        .send_bytes(&body)
        .map_err(|e| anyhow::anyhow!("POST initializeValidation: {e}"))?;
    let mut resp_buf = Vec::new();
    resp.into_reader().read_to_end(&mut resp_buf)?;

    let rv: plist::Value = plist::from_bytes(&resp_buf)?;
    let session_info = rv
        .as_dictionary()
        .and_then(|d| d.get("session-info"))
        .and_then(|v| v.as_data())
        .map(|d| d.to_vec())
        .ok_or_else(|| anyhow::anyhow!("response: missing 'session-info'"))?;

    ctx.key_establishment(&session_info)
        .map_err(|e| anyhow::anyhow!("key_establishment: {e}"))?;
    ctx.sign().map_err(|e| anyhow::anyhow!("nac_sign: {e}"))
}

#[derive(Deserialize)]
pub struct Store {
    pub profiles: std::collections::HashMap<String, Profile>,
}
