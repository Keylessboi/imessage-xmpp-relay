//! Minimal hex helpers (no extra dependency for two functions).

/// Lowercase hex encoding.
pub fn encode_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// Decode hex, tolerating ':','-','_',whitespace separators. Returns `None` on a
/// non-hex character or an odd number of digits.
pub fn decode_hex(s: &str) -> Option<Vec<u8>> {
    let digits: Vec<u8> = s
        .bytes()
        .filter(|b| !matches!(b, b':' | b'-' | b' ' | b'\n' | b'\r' | b'\t' | b'_'))
        .collect();
    if digits.is_empty() || digits.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(digits.len() / 2);
    for pair in digits.chunks(2) {
        let hi = (pair[0] as char).to_digit(16)?;
        let lo = (pair[1] as char).to_digit(16)?;
        out.push(((hi << 4) | lo) as u8);
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let b = [0u8, 1, 15, 16, 255];
        assert_eq!(encode_hex(&b), "00010f10ff");
        assert_eq!(decode_hex("00010f10ff").unwrap(), b);
    }

    #[test]
    fn tolerates_separators() {
        assert_eq!(decode_hex("00:01-0f 10").unwrap(), vec![0, 1, 15, 16]);
        assert!(decode_hex("abc").is_none());
        assert!(decode_hex("zz").is_none());
    }
}
