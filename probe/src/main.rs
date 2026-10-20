use std::io::Write;
use std::process::{Command, Stdio};
use std::{env, fs};

use open_absinthe::nac::{HardwareConfig, ValidationCtx};
use plist::Value;

fn dummy_hw() -> HardwareConfig {
    HardwareConfig {
        product_name: "MacBookPro16,1".to_string(),
        io_mac_address: [0x00, 0x1b, 0x21, 0xaa, 0xbb, 0xcc],
        platform_serial_number: "C02ZK0AAAAAA".to_string(),
        platform_uuid: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C5D".to_string(),
        root_disk_uuid: "11223344-5566-7788-99AA-BBCCDDEEFF00".to_string(),
        board_id: "Mac-E1008331FDC96864".to_string(),
        os_build_num: "21G93".to_string(),
        platform_serial_number_enc: vec![0x01, 0x02, 0x03, 0x04],
        platform_uuid_enc: vec![0x05, 0x06, 0x07, 0x08],
        root_disk_uuid_enc: vec![0x09, 0x0a, 0x0b, 0x0c],
        rom: vec![0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff],
        rom_enc: vec![0x0d, 0x0e, 0x0f, 0x10],
        mlb: "C02ZK0AAAAAAA1A".to_string(),
        mlb_enc: vec![0x11, 0x12, 0x13, 0x14],
    }
}

fn curl_post(url: &str, ctype: &str, body: &[u8]) -> Result<Vec<u8>, String> {
    let mut child = Command::new("curl")
        .args(["-sk", "--max-time", "60", "-X", "POST",
               "-H", &format!("Content-Type: {ctype}"),
               "--data-binary", "@-", url])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| e.to_string())?;
    child.stdin.as_mut().unwrap().write_all(body).map_err(|e| e.to_string())?;
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!("curl exit {:?}: {}", out.status.code(), String::from_utf8_lossy(&out.stderr)));
    }
    Ok(out.stdout)
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let cert = fs::read(&args[1]).expect("cert file");
    let init_url = &args[2];
    let hw = dummy_hw();

    println!("[*] cert chain: {} bytes", cert.len());
    let mut req = Vec::new();
    let mut ctx = match ValidationCtx::new(&cert, &mut req, &hw) {
        Ok(c) => c,
        Err(e) => { println!("[!] nac_init FAILED: {e}"); std::process::exit(2); }
    };
    println!("[+] nac_init OK -> session-info-request = {} bytes", req.len());

    let mut dict = plist::Dictionary::new();
    dict.insert("session-info-request".into(), Value::Data(req));
    let mut body = Vec::new();
    plist::to_writer_xml(&mut body, &Value::Dictionary(dict)).expect("plist write");
    println!("[*] POST {} ({} bytes)", init_url, body.len());

    let resp = match curl_post(init_url, "application/x-apple-plist", &body) {
        Ok(r) => r,
        Err(e) => { println!("[!] POST failed: {e}"); std::process::exit(3); }
    };
    println!("[+] HTTP response: {} bytes", resp.len());

    let val: Value = match plist::from_bytes(&resp) {
        Ok(v) => v,
        Err(e) => { println!("[!] response not plist: {e}\n    raw: {}", String::from_utf8_lossy(&resp[..resp.len().min(400)])); std::process::exit(3); }
    };
    let si = val.as_dictionary().and_then(|d| d.get("session-info")).and_then(|v| v.as_data())
        .map(|d| d.to_vec())
        .unwrap_or_else(|| { println!("[!] no session-info key; keys: {:?}", val.as_dictionary().map(|d| d.keys().collect::<Vec<_>>())); std::process::exit(3); });
    println!("[+] session-info: {} bytes", si.len());

    if let Err(e) = ctx.key_establishment(&si) {
        println!("[!] key_establishment FAILED: {e}"); std::process::exit(4);
    }
    println!("[+] key_establishment OK");

    match ctx.sign() {
        Ok(sig) => {
            let hex: String = sig.iter().take(24).map(|b| format!("{b:02x}")).collect();
            println!("[+] nac_sign OK -> VALIDATION DATA = {} bytes", sig.len());
            println!("    first24hex: {hex}");
            println!("\nRESULT: EMULATOR WORKS END-TO-END");
        }
        Err(e) => { println!("[!] sign FAILED: {e}"); std::process::exit(5); }
    }
}
