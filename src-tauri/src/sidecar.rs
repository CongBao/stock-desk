use std::{
    collections::BTreeMap,
    fmt,
    net::{Ipv4Addr, TcpListener},
    path::{Path, PathBuf},
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};

const PRODUCT_VERSION: &str = "1.1.0";
const TAURI_ORIGIN: &str = "http://tauri.localhost";

#[derive(Clone)]
pub struct SidecarAuthority {
    port: u16,
    data_root: PathBuf,
    source_revision: String,
    secret: String,
}

impl SidecarAuthority {
    pub fn new(local_data_root: &Path, source_revision: &str) -> Result<Self, String> {
        if !local_data_root.is_absolute() {
            return Err("current-user data root is unavailable".into());
        }
        if source_revision.len() != 40
            || !source_revision.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("source identity is unavailable".into());
        }
        let mut secret_bytes = [0_u8; 32];
        getrandom::fill(&mut secret_bytes)
            .map_err(|_| "session authority is unavailable".to_owned())?;
        Ok(Self {
            port: reserve_loopback_port()?,
            data_root: local_data_root.join("Stock Desk").join("v1.1"),
            source_revision: source_revision.to_ascii_lowercase(),
            secret: URL_SAFE_NO_PAD.encode(secret_bytes),
        })
    }

    pub fn handshake_url(&self) -> String {
        self.api_url("/api/desktop/handshake")
            .expect("fixed handshake path must be valid")
    }

    pub(crate) fn api_url(&self, path: &str) -> Result<String, &'static str> {
        if !path.starts_with("/api") {
            return Err("desktop_proxy_invalid_path");
        }
        Ok(format!("http://127.0.0.1:{}{path}", self.port))
    }

    pub fn origin(&self) -> &'static str {
        TAURI_ORIGIN
    }

    pub fn authorization_header(&self) -> String {
        format!("Bearer {}", self.secret)
    }

    pub fn source_revision(&self) -> &str {
        &self.source_revision
    }

    pub fn environment(&self) -> BTreeMap<String, String> {
        BTreeMap::from([
            ("STOCK_DESK_DESKTOP_PORT".into(), self.port.to_string()),
            ("STOCK_DESK_DESKTOP_ORIGIN".into(), TAURI_ORIGIN.into()),
            (
                "STOCK_DESK_DESKTOP_SESSION_SECRET".into(),
                self.secret.clone(),
            ),
            (
                "STOCK_DESK_DESKTOP_DATA_ROOT".into(),
                self.data_root.to_string_lossy().into_owned(),
            ),
            (
                "STOCK_DESK_DESKTOP_HOST_VERSION".into(),
                PRODUCT_VERSION.into(),
            ),
            (
                "STOCK_DESK_DESKTOP_FRONTEND_VERSION".into(),
                PRODUCT_VERSION.into(),
            ),
            (
                "STOCK_DESK_DESKTOP_SIDECAR_VERSION".into(),
                PRODUCT_VERSION.into(),
            ),
            (
                "STOCK_DESK_DESKTOP_SOURCE_REVISION".into(),
                self.source_revision.clone(),
            ),
        ])
    }
}

impl fmt::Debug for SidecarAuthority {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SidecarAuthority")
            .field("port", &self.port)
            .field("data_root", &"<private-current-user-data>")
            .field("source_revision", &self.source_revision)
            .field("secret", &"<redacted>")
            .finish()
    }
}

fn reserve_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .map_err(|_| "loopback port is unavailable".to_owned())?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|_| "loopback port is unavailable".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn authority_uses_random_high_entropy_secret_and_versioned_user_root() {
        let root = std::env::temp_dir().join("stock-desk-user-root");
        let first = SidecarAuthority::new(&root, &"a".repeat(40)).unwrap();
        let second = SidecarAuthority::new(&root, &"a".repeat(40)).unwrap();

        assert_ne!(first.secret, second.secret);
        assert_eq!(URL_SAFE_NO_PAD.decode(&first.secret).unwrap().len(), 32);
        assert_eq!(first.data_root, root.join("Stock Desk").join("v1.1"));
        assert_ne!(first.port, 0);
        assert!(first.handshake_url().starts_with("http://127.0.0.1:"));
    }

    #[test]
    fn authority_environment_contains_no_arguments_or_mutable_origin() {
        let root = std::env::temp_dir().join("stock-desk-user-root");
        let authority = SidecarAuthority::new(&root, &"b".repeat(40)).unwrap();
        let environment = authority.environment();

        assert_eq!(environment.len(), 8);
        assert_eq!(
            environment.get("STOCK_DESK_DESKTOP_ORIGIN").unwrap(),
            TAURI_ORIGIN
        );
        assert_eq!(
            environment
                .get("STOCK_DESK_DESKTOP_SOURCE_REVISION")
                .unwrap(),
            &"b".repeat(40)
        );
        assert!(!format!("{authority:?}").contains(&authority.secret));
        assert!(!format!("{authority:?}").contains(root.to_string_lossy().as_ref()));
    }

    #[test]
    fn authority_rejects_relative_root_and_unknown_revision() {
        assert!(SidecarAuthority::new(Path::new("relative"), &"a".repeat(40)).is_err());
        assert!(SidecarAuthority::new(Path::new("/tmp"), "unknown").is_err());
    }
}
