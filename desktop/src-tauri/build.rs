fn main() {
    if matches!(
        std::env::var("CARGO_CFG_TARGET_OS").as_deref(),
        Ok("windows")
    ) && matches!(std::env::var("CARGO_CFG_TARGET_ENV").as_deref(), Ok("msvc"))
    {
        // Both production and fixture-authority desktop links use this owner.
        println!("cargo:rustc-link-arg-bin=eom-email-watcher-desktop=/Brepro");
    }
    tauri_build::build()
}
