fn main() {
    // Unicorn bundles QEMU, whose cputlb.c uses 16-byte atomics
    // (__atomic_load_16 / __atomic_store_16 / __atomic_compare_exchange_16).
    // On x86_64 these live in libatomic, which rustc does not link by default.
    println!("cargo:rustc-link-lib=atomic");
}
