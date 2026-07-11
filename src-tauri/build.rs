use std::process::Command;

fn main() {
    tauri_build::build();
    println!("cargo:rerun-if-env-changed=STOCK_DESK_SOURCE_REVISION");
    println!("cargo:rerun-if-changed=../.git/HEAD");
    let revision = std::env::var("STOCK_DESK_SOURCE_REVISION")
        .ok()
        .or_else(|| {
            Command::new("git")
                .args(["rev-parse", "HEAD"])
                .output()
                .ok()
                .filter(|output| output.status.success())
                .and_then(|output| String::from_utf8(output.stdout).ok())
                .map(|output| output.trim().to_owned())
        })
        .expect("an exact source revision is required to build Stock Desk");
    assert!(
        revision.len() == 40 && revision.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "Stock Desk source revision must be a 40-character Git SHA"
    );
    println!(
        "cargo:rustc-env=STOCK_DESK_SOURCE_REVISION={}",
        revision.to_ascii_lowercase()
    );
}
