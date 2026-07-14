//! Strict, privacy-preserving transport helpers for the desktop updater.
//!
//! This module deliberately contains no network side effects.  The updater
//! controller owns the `reqwest`/Tauri calls and feeds their public response
//! data into these helpers before accepting metadata, bytes, redirects, or a
//! Tauri-compatible Minisign signature.

use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, ACCEPT_ENCODING, USER_AGENT};
use semver::Version;
use url::Url;

pub const LATEST_METADATA_URL: &str =
    "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json";
pub const MAX_METADATA_BYTES: usize = 32 * 1024;
pub const MAX_ASSET_BYTES: usize = 512 * 1024 * 1024;

const MAX_URL_BYTES: usize = 2 * 1024;
const MAX_CONTENT_TYPE_BYTES: usize = 128;
const FIXED_USER_AGENT: &str = "stock-desk-updater";
const REPOSITORY_RELEASE_PREFIX: &str = "/CongBao/stock-desk/releases/download/v";
const ASSET_SUFFIX: &str = "-windows-x64-setup.exe";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestKind {
    Metadata,
    Asset,
}

impl RequestKind {
    pub const fn maximum_bytes(self) -> usize {
        match self {
            Self::Metadata => MAX_METADATA_BYTES,
            Self::Asset => MAX_ASSET_BYTES,
        }
    }

    const fn accept(self) -> &'static str {
        match self {
            Self::Metadata => "application/json, application/octet-stream;q=0.9",
            Self::Asset => "application/octet-stream",
        }
    }
}

/// Build the complete set of explicit updater request headers.
///
/// The value is deliberately stable across users and application versions. It
/// contains no device identifier, locale, session token, referrer, or cookie.
/// `identity` encoding also keeps byte limits tied to the downloaded bytes.
pub fn anonymous_headers(kind: RequestKind) -> HeaderMap {
    let mut headers = HeaderMap::with_capacity(3);
    headers.insert(ACCEPT, HeaderValue::from_static(kind.accept()));
    headers.insert(ACCEPT_ENCODING, HeaderValue::from_static("identity"));
    headers.insert(USER_AGENT, HeaderValue::from_static(FIXED_USER_AGENT));
    headers
}

/// Reject accidental additions to the explicit anonymous header policy.
pub fn validate_anonymous_headers(
    kind: RequestKind,
    headers: &HeaderMap,
) -> Result<(), &'static str> {
    if headers != &anonymous_headers(kind) {
        return Err("desktop_updater_request_headers_rejected");
    }
    Ok(())
}

/// Validate the two confined redirects for the mutable `latest.json` endpoint.
///
/// The first redirect resolves the release alias to the repository's immutable,
/// exact-version metadata asset. The second may move those exact bytes to
/// GitHub's release CDN. No other hop or origin is accepted.
pub fn validate_metadata_redirect(
    from: &str,
    to: &str,
    version: &str,
    hop: usize,
) -> Result<(), &'static str> {
    validate_stable_version(version).map_err(|_| "desktop_updater_metadata_redirect_rejected")?;
    match hop {
        0 if from == LATEST_METADATA_URL => validate_metadata_final_url(to, version),
        1 if validate_metadata_final_url(from, version).is_ok() => {
            validate_release_cdn_url(to, "desktop_updater_metadata_redirect_rejected")
        }
        _ => Err("desktop_updater_metadata_redirect_rejected"),
    }
}

/// Validate the final immutable metadata URL after redirect handling.
pub fn validate_metadata_final_url(value: &str, version: &str) -> Result<(), &'static str> {
    validate_stable_version(version).map_err(|_| "desktop_updater_metadata_redirect_rejected")?;
    let url = strict_https_url(value).map_err(|_| "desktop_updater_metadata_redirect_rejected")?;
    let expected_path = format!("{REPOSITORY_RELEASE_PREFIX}{version}/latest.json");
    if url.host_str() != Some("github.com")
        || url.path() != expected_path
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("desktop_updater_metadata_redirect_rejected");
    }
    Ok(())
}

/// Validate the repository-owned asset URL announced by trusted metadata.
pub fn validate_repository_asset_url(value: &str, version: &str) -> Result<(), &'static str> {
    validate_stable_version(version).map_err(|_| "desktop_updater_asset_url_rejected")?;
    let url = strict_https_url(value).map_err(|_| "desktop_updater_asset_url_rejected")?;
    let expected_path =
        format!("{REPOSITORY_RELEASE_PREFIX}{version}/stock-desk-{version}{ASSET_SUFFIX}");
    if url.host_str() != Some("github.com")
        || url.path() != expected_path
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("desktop_updater_asset_url_rejected");
    }
    Ok(())
}

/// Constrain the one redirect used by GitHub release-asset downloads.
///
/// The signed CDN query is intentionally opaque, but it may only be reached
/// from the exact repository/version asset URL and only on GitHub's dedicated
/// release-asset host. A CDN redirect or cross-origin second hop is rejected.
pub fn validate_asset_redirect(
    from: &str,
    to: &str,
    version: &str,
    hop: usize,
) -> Result<(), &'static str> {
    if hop != 0 || validate_repository_asset_url(from, version).is_err() {
        return Err("desktop_updater_asset_redirect_rejected");
    }
    validate_release_cdn_url(to, "desktop_updater_asset_redirect_rejected")
}

/// Check response metadata before any body is accumulated.
///
/// A missing `Content-Length` is allowed because GitHub may stream, but every
/// chunk must still pass [`checked_body_length`]. A missing or ambiguous media
/// type is rejected.
pub fn validate_response_headers(
    kind: RequestKind,
    content_type: Option<&str>,
    content_length: Option<u64>,
) -> Result<(), &'static str> {
    validate_content_type(kind, content_type)?;
    if let Some(length) = content_length {
        if length == 0 || length > kind.maximum_bytes() as u64 {
            return Err("desktop_updater_response_size_rejected");
        }
    }
    Ok(())
}

/// Return the new body length while enforcing overflow-safe streaming limits.
pub fn checked_body_length(
    kind: RequestKind,
    accumulated: usize,
    incoming: usize,
) -> Result<usize, &'static str> {
    let total = accumulated
        .checked_add(incoming)
        .ok_or("desktop_updater_response_size_rejected")?;
    if total > kind.maximum_bytes() {
        return Err("desktop_updater_response_size_rejected");
    }
    Ok(total)
}

pub fn validate_complete_body(kind: RequestKind, length: usize) -> Result<(), &'static str> {
    if length == 0 || length > kind.maximum_bytes() {
        return Err("desktop_updater_response_size_rejected");
    }
    Ok(())
}

fn validate_content_type(kind: RequestKind, value: Option<&str>) -> Result<(), &'static str> {
    let value = value.ok_or("desktop_updater_content_type_rejected")?;
    if value.is_empty()
        || value.len() > MAX_CONTENT_TYPE_BYTES
        || !value.is_ascii()
        || value.bytes().any(|byte| byte.is_ascii_control())
    {
        return Err("desktop_updater_content_type_rejected");
    }
    let mut segments = value.split(';');
    let media_type = segments.next().unwrap_or_default().trim();
    let parameters: Vec<_> = segments.map(str::trim).collect();
    let accepted = match kind {
        RequestKind::Metadata => {
            (media_type.eq_ignore_ascii_case("application/json")
                && (parameters.is_empty()
                    || parameters.len() == 1
                        && parameters[0].eq_ignore_ascii_case("charset=utf-8")))
                || (media_type.eq_ignore_ascii_case("application/octet-stream")
                    && parameters.is_empty())
        }
        RequestKind::Asset => {
            parameters.is_empty()
                && [
                    "application/octet-stream",
                    "application/x-msdownload",
                    "application/vnd.microsoft.portable-executable",
                ]
                .iter()
                .any(|candidate| media_type.eq_ignore_ascii_case(candidate))
        }
    };
    if !accepted {
        return Err("desktop_updater_content_type_rejected");
    }
    Ok(())
}

fn strict_https_url(value: &str) -> Result<Url, &'static str> {
    if value.is_empty() || value.len() > MAX_URL_BYTES || value.trim() != value {
        return Err("desktop_updater_url_rejected");
    }
    let url = Url::parse(value).map_err(|_| "desktop_updater_url_rejected")?;
    if url.scheme() != "https"
        || url.cannot_be_a_base()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
    {
        return Err("desktop_updater_url_rejected");
    }
    Ok(url)
}

fn validate_release_cdn_url(value: &str, error: &'static str) -> Result<(), &'static str> {
    let target = strict_https_url(value).map_err(|_| error)?;
    if target.host_str() != Some("release-assets.githubusercontent.com")
        || !target
            .path()
            .starts_with("/github-production-release-asset/")
        || target.path() == "/github-production-release-asset/"
        || target.query().is_none()
        || target.fragment().is_some()
    {
        return Err(error);
    }
    Ok(())
}

fn validate_stable_version(value: &str) -> Result<Version, &'static str> {
    let version = Version::parse(value).map_err(|_| "desktop_updater_version_rejected")?;
    if !version.pre.is_empty()
        || !version.build.is_empty()
        || version.to_string() != value
        || value.split('.').count() != 3
    {
        return Err("desktop_updater_version_rejected");
    }
    Ok(version)
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::header::{AUTHORIZATION, COOKIE, REFERER};

    const VERSION: &str = "1.2.0";

    fn asset_url() -> String {
        format!(
            "https://github.com/CongBao/stock-desk/releases/download/v{VERSION}/stock-desk-{VERSION}-windows-x64-setup.exe"
        )
    }

    #[test]
    fn anonymous_headers_are_fixed_and_contain_no_user_identity() {
        for kind in [RequestKind::Metadata, RequestKind::Asset] {
            let headers = anonymous_headers(kind);
            assert_eq!(headers.len(), 3);
            assert_eq!(headers[ACCEPT_ENCODING], "identity");
            assert_eq!(headers[USER_AGENT], FIXED_USER_AGENT);
            assert!(!headers.contains_key(AUTHORIZATION));
            assert!(!headers.contains_key(COOKIE));
            assert!(!headers.contains_key(REFERER));
            assert!(validate_anonymous_headers(kind, &headers).is_ok());
        }
    }

    #[test]
    fn anonymous_header_policy_rejects_any_extra_or_changed_header() {
        let mut headers = anonymous_headers(RequestKind::Metadata);
        headers.insert(AUTHORIZATION, HeaderValue::from_static("Bearer secret"));
        assert_eq!(
            validate_anonymous_headers(RequestKind::Metadata, &headers),
            Err("desktop_updater_request_headers_rejected")
        );

        let asset_headers = anonymous_headers(RequestKind::Asset);
        assert_eq!(
            validate_anonymous_headers(RequestKind::Metadata, &asset_headers),
            Err("desktop_updater_request_headers_rejected")
        );
    }

    #[test]
    fn metadata_redirects_are_exact_repository_version_and_release_cdn() {
        let immutable = format!(
            "https://github.com/CongBao/stock-desk/releases/download/v{VERSION}/latest.json"
        );
        let cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/123/metadata?sp=r&sig=opaque";
        assert!(validate_metadata_redirect(LATEST_METADATA_URL, &immutable, VERSION, 0).is_ok());
        assert!(validate_metadata_redirect(&immutable, cdn, VERSION, 1).is_ok());
        for (from, to, version, hop) in [
            (LATEST_METADATA_URL, immutable.as_str(), VERSION, 2),
            (immutable.as_str(), immutable.as_str(), VERSION, 0),
            (LATEST_METADATA_URL, "http://github.com/unsafe", VERSION, 0),
            (
                LATEST_METADATA_URL,
                "https://evil.example/latest.json",
                VERSION,
                0,
            ),
            (LATEST_METADATA_URL, immutable.as_str(), "1.2.0+build", 0),
            (
                immutable.as_str(),
                "https://objects.githubusercontent.com/object?sig=x",
                VERSION,
                1,
            ),
        ] {
            assert!(validate_metadata_redirect(from, to, version, hop).is_err());
        }
        assert!(
            validate_metadata_final_url(&format!("{immutable}?token=unexpected"), VERSION).is_err()
        );
    }

    #[test]
    fn asset_redirect_is_confined_to_the_github_release_cdn() {
        let source = asset_url();
        let cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/123/asset?sp=r&sig=opaque";
        assert!(validate_asset_redirect(&source, cdn, VERSION, 0).is_ok());
        for (target, hop) in [
            ("https://objects.githubusercontent.com/object?sig=x", 0),
            ("https://release-assets.githubusercontent.com/not-release/asset?sig=x", 0),
            ("https://release-assets.githubusercontent.com/github-production-release-asset/123/asset", 0),
            (cdn, 1),
        ] {
            assert!(validate_asset_redirect(&source, target, VERSION, hop).is_err());
        }
        assert!(validate_asset_redirect(
            "https://github.com/another/repository/releases/download/v1.2.0/file.exe",
            cdn,
            VERSION,
            0
        )
        .is_err());
    }

    #[test]
    fn response_policy_enforces_media_type_and_declared_size() {
        assert!(validate_response_headers(
            RequestKind::Metadata,
            Some("application/json; charset=UTF-8"),
            Some(MAX_METADATA_BYTES as u64)
        )
        .is_ok());
        assert!(validate_response_headers(
            RequestKind::Metadata,
            Some("application/octet-stream"),
            None
        )
        .is_ok());
        assert!(validate_response_headers(
            RequestKind::Asset,
            Some("application/x-msdownload"),
            Some(MAX_ASSET_BYTES as u64)
        )
        .is_ok());
        for content_type in [
            None,
            Some("text/html"),
            Some("application/json; charset=latin1"),
        ] {
            assert!(
                validate_response_headers(RequestKind::Metadata, content_type, Some(1)).is_err()
            );
        }
        assert!(validate_response_headers(
            RequestKind::Asset,
            Some("application/octet-stream"),
            Some(MAX_ASSET_BYTES as u64 + 1)
        )
        .is_err());
        assert!(validate_response_headers(
            RequestKind::Asset,
            Some("application/octet-stream"),
            Some(0)
        )
        .is_err());
    }

    #[test]
    fn streamed_body_limit_is_overflow_safe_and_requires_content() {
        assert_eq!(
            checked_body_length(RequestKind::Metadata, MAX_METADATA_BYTES - 1, 1),
            Ok(MAX_METADATA_BYTES)
        );
        assert!(checked_body_length(RequestKind::Metadata, MAX_METADATA_BYTES, 1).is_err());
        assert!(checked_body_length(RequestKind::Asset, usize::MAX, 1).is_err());
        assert!(validate_complete_body(RequestKind::Asset, 1).is_ok());
        assert!(validate_complete_body(RequestKind::Asset, 0).is_err());
    }
}
