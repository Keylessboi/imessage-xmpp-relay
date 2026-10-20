fn main() {
    // unicorn-engine bundles QEMU, whose cputlb.c uses 16-byte atomics
    // (__atomic_load_16 / __atomic_store_16). Those live in libatomic on
    // x86_64, which rustc does not link by default, so the final binary fails
    // to link without this. Same fix as crates/openbubbles-relay/build.rs.
    println!("cargo:rustc-link-lib=atomic");
}
