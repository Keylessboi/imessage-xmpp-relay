fn main() {
    // Unicorn bundles QEMU; its cputlb.c uses 16-byte atomics that live in
    // libatomic on x86_64, which rustc does not link by default.
    println!("cargo:rustc-link-lib=atomic");
}
