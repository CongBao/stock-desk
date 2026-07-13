use serde::{Deserialize, Serialize};
use tauri::Manager;

use crate::app::DesktopRuntime;

const MAX_PATH_BYTES: usize = 4 * 1024;
const MAX_REQUEST_BODY_BYTES: usize = 1024 * 1024;
// The public formula contract permits a 128 MiB SignalSeries. Market chart
// responses can carry that series together with up to 100,000 bars, so the
// desktop proxy needs bounded headroom without narrowing the v1 API contract.
const MAX_RESPONSE_BODY_BYTES: usize = 192 * 1024 * 1024;
const MAX_CONTENT_TYPE_BYTES: usize = 128;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopApiRequest {
    method: String,
    path: String,
    body: Option<String>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct DesktopApiResponse {
    status: u16,
    content_type: String,
    body: String,
}

#[derive(Debug)]
struct ValidatedRequest {
    method: reqwest::Method,
    path: String,
    body: Option<String>,
}

#[tauri::command]
pub async fn desktop_api_request(
    app: tauri::AppHandle,
    request: DesktopApiRequest,
) -> Result<DesktopApiResponse, String> {
    let request = validate_request(request).map_err(str::to_owned)?;
    let runtime = app
        .try_state::<DesktopRuntime>()
        .ok_or_else(|| "desktop_runtime_not_ready".to_owned())?;
    let session = runtime.ready_session().map_err(str::to_owned)?;
    let url = session
        .authority
        .api_url(&request.path)
        .map_err(str::to_owned)?;

    let mut outbound = session
        .client
        .request(request.method, url)
        .header("Origin", session.authority.origin())
        .header("Authorization", session.authority.authorization_header())
        .header("Accept", "application/json");
    if let Some(body) = request.body {
        outbound = outbound
            .header("Content-Type", "application/json")
            .body(body);
    }
    let response = outbound
        .send()
        .await
        .map_err(|_| "desktop_proxy_unavailable".to_owned())?;
    let status = response.status().as_u16();
    let headers = response.headers().clone();
    if response
        .content_length()
        .is_some_and(|length| length > MAX_RESPONSE_BODY_BYTES as u64)
    {
        return Err("desktop_proxy_response_too_large".to_owned());
    }
    let body = read_bounded_body(response).await?;
    let response = finalize_response(status, &headers, body)?;
    if !runtime.is_same_ready_generation(session.generation) {
        return Err("desktop_runtime_not_ready".to_owned());
    }
    Ok(response)
}

fn validate_request(request: DesktopApiRequest) -> Result<ValidatedRequest, &'static str> {
    let method = match request.method.as_str() {
        "DELETE" => reqwest::Method::DELETE,
        "GET" => reqwest::Method::GET,
        "POST" => reqwest::Method::POST,
        "PUT" => reqwest::Method::PUT,
        _ => return Err("desktop_proxy_invalid_method"),
    };
    validate_path(&request.path)?;
    if let Some(body) = request.body.as_deref() {
        if body.len() > MAX_REQUEST_BODY_BYTES {
            return Err("desktop_proxy_request_too_large");
        }
        serde_json::from_str::<serde_json::Value>(body)
            .map_err(|_| "desktop_proxy_invalid_json")?;
    }
    Ok(ValidatedRequest {
        method,
        path: request.path,
        body: request.body,
    })
}

fn validate_path(path: &str) -> Result<(), &'static str> {
    if path.is_empty()
        || path.len() > MAX_PATH_BYTES
        || path.contains('\\')
        || path.contains('#')
        || path.bytes().any(|byte| byte.is_ascii_control())
    {
        return Err("desktop_proxy_invalid_path");
    }
    let path_part = path.split_once('?').map_or(path, |(path, _)| path);
    if path_part != "/api" && !path_part.starts_with("/api/") {
        return Err("desktop_proxy_invalid_path");
    }
    let decoded = percent_decode(path_part)?;
    if decoded.split(|byte| *byte == b'/').any(|segment| {
        segment == b"." || segment == b".." || segment.contains(&b'\\') || segment.contains(&0)
    }) {
        return Err("desktop_proxy_invalid_path");
    }
    Ok(())
}

fn percent_decode(value: &str) -> Result<Vec<u8>, &'static str> {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'%' {
            decoded.push(bytes[index]);
            index += 1;
            continue;
        }
        if index + 2 >= bytes.len() {
            return Err("desktop_proxy_invalid_path");
        }
        let high = hex_value(bytes[index + 1]).ok_or("desktop_proxy_invalid_path")?;
        let low = hex_value(bytes[index + 2]).ok_or("desktop_proxy_invalid_path")?;
        let decoded_byte = (high << 4) | low;
        if matches!(decoded_byte, b'/' | b'\\' | 0) {
            return Err("desktop_proxy_invalid_path");
        }
        decoded.push(decoded_byte);
        index += 3;
    }
    Ok(decoded)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn validated_content_type(headers: &reqwest::header::HeaderMap) -> Result<String, &'static str> {
    let value = headers
        .get(reqwest::header::CONTENT_TYPE)
        .ok_or("desktop_proxy_invalid_response")?
        .to_str()
        .map_err(|_| "desktop_proxy_invalid_response")?;
    if value.len() > MAX_CONTENT_TYPE_BYTES {
        return Err("desktop_proxy_invalid_response");
    }
    let media_type = value
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if media_type != "application/json"
        && !(media_type.starts_with("application/") && media_type.ends_with("+json"))
    {
        return Err("desktop_proxy_invalid_response");
    }
    Ok(value.to_owned())
}

fn finalize_response(
    status: u16,
    headers: &reqwest::header::HeaderMap,
    body: String,
) -> Result<DesktopApiResponse, String> {
    if status == 204 {
        if !body.is_empty() {
            return Err("desktop_proxy_invalid_response".to_owned());
        }
        return Ok(DesktopApiResponse {
            status,
            content_type: "application/json".to_owned(),
            body,
        });
    }
    let content_type = validated_content_type(headers).map_err(str::to_owned)?;
    if serde_json::from_str::<serde_json::Value>(&body).is_err() {
        return Err("desktop_proxy_invalid_response".to_owned());
    }
    Ok(DesktopApiResponse {
        status,
        content_type,
        body,
    })
}

async fn read_bounded_body(mut response: reqwest::Response) -> Result<String, String> {
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| "desktop_proxy_unavailable".to_owned())?
    {
        append_bounded(&mut body, &chunk)?;
    }
    String::from_utf8(body).map_err(|_| "desktop_proxy_invalid_response".to_owned())
}

fn append_bounded(body: &mut Vec<u8>, chunk: &[u8]) -> Result<(), String> {
    if response_body_would_exceed_limit(body.len(), chunk.len()) {
        return Err("desktop_proxy_response_too_large".to_owned());
    }
    body.extend_from_slice(chunk);
    Ok(())
}

fn response_body_would_exceed_limit(current: usize, incoming: usize) -> bool {
    current.saturating_add(incoming) > MAX_RESPONSE_BODY_BYTES
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(method: &str, path: &str, body: Option<&str>) -> DesktopApiRequest {
        DesktopApiRequest {
            method: method.into(),
            path: path.into(),
            body: body.map(str::to_owned),
        }
    }

    #[test]
    fn accepts_only_the_closed_request_contract() {
        let validated =
            validate_request(request("POST", "/api/v1/watchlists?limit=20", Some("{}")))
                .expect("valid API request");
        assert_eq!(validated.method, reqwest::Method::POST);
        assert_eq!(validated.path, "/api/v1/watchlists?limit=20");

        assert_eq!(
            validate_request(request("PATCH", "/api/v1/watchlists", None)).unwrap_err(),
            "desktop_proxy_invalid_method"
        );
        assert_eq!(
            validate_request(request("POST", "/api/v1/watchlists", Some("not-json"))).unwrap_err(),
            "desktop_proxy_invalid_json"
        );
        assert!(
            serde_json::from_value::<DesktopApiRequest>(serde_json::json!({
                "method": "GET",
                "path": "/api/status",
                "body": null,
                "headers": {"X-Forwarded-Host": "attacker.invalid"}
            }))
            .is_err()
        );
    }

    #[test]
    fn accepts_core_workflow_paths_without_frontend_authority() {
        let cases = [
            request(
                "POST",
                "/api/backtests/preflight",
                Some(r#"{"formula_version_id":"version","period":"1d"}"#),
            ),
            request(
                "POST",
                "/api/backtests",
                Some(r#"{"formula_version_id":"version","period":"1d"}"#),
            ),
            request(
                "POST",
                "/api/analysis",
                Some(r#"{"symbol":"600000.SH","model_config_id":"model"}"#),
            ),
            request("GET", "/api/tasks?view=safe&limit=100", None),
            request("GET", "/api/tasks/task-id/events?view=safe&limit=100", None),
        ];

        for case in cases {
            let validated = validate_request(case).expect("core workflow path");
            assert!(validated.path.starts_with("/api/"));
            assert!(validated.body.as_deref().is_none_or(|body| {
                let value: serde_json::Value =
                    serde_json::from_str(body).expect("validated JSON body");
                value.get("authorization").is_none()
                    && value.get("session_secret").is_none()
                    && value.get("port").is_none()
            }));
        }
    }

    #[test]
    fn rejects_absolute_cross_origin_and_traversal_paths() {
        for path in [
            "https://attacker.invalid/api",
            "//attacker.invalid/api",
            "/other",
            "/api/../secret",
            "/api/%2e%2e/secret",
            "/api/%2E%2E%2Fsecret",
            "/api/%2f%2fattacker.invalid",
            "/api\\..\\secret",
            "/api/value#fragment",
            "/api/%ZZ",
        ] {
            assert_eq!(
                validate_path(path),
                Err("desktop_proxy_invalid_path"),
                "{path}"
            );
        }
    }

    #[test]
    fn enforces_request_size_before_forwarding() {
        let oversized = format!("\"{}\"", "x".repeat(MAX_REQUEST_BODY_BYTES));
        assert_eq!(
            validate_request(request("PUT", "/api/item", Some(&oversized))).unwrap_err(),
            "desktop_proxy_request_too_large"
        );
    }

    #[test]
    fn response_wire_format_contains_only_public_fields() {
        assert_eq!(
            serde_json::to_value(DesktopApiResponse {
                status: 200,
                content_type: "application/json".into(),
                body: "{\"ok\":true}".into(),
            })
            .unwrap(),
            serde_json::json!({
                "status": 200,
                "content_type": "application/json",
                "body": "{\"ok\":true}"
            })
        );
    }

    #[test]
    fn permits_only_bounded_json_response_types() {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            reqwest::header::CONTENT_TYPE,
            "application/problem+json; charset=utf-8".parse().unwrap(),
        );
        assert!(validated_content_type(&headers).is_ok());
        headers.insert(reqwest::header::CONTENT_TYPE, "text/html".parse().unwrap());
        assert_eq!(
            validated_content_type(&headers),
            Err("desktop_proxy_invalid_response")
        );
        headers.insert(
            reqwest::header::CONTENT_TYPE,
            "text/problem+json".parse().unwrap(),
        );
        assert_eq!(
            validated_content_type(&headers),
            Err("desktop_proxy_invalid_response")
        );
    }

    #[test]
    fn response_accumulation_stops_at_the_hard_limit() {
        assert!(!response_body_would_exceed_limit(
            MAX_RESPONSE_BODY_BYTES - 1,
            1
        ));
        assert!(response_body_would_exceed_limit(MAX_RESPONSE_BODY_BYTES, 1));
        assert!(response_body_would_exceed_limit(usize::MAX, 1));

        let mut body = b"bounded".to_vec();
        append_bounded(&mut body, b" response").unwrap();
        assert_eq!(body, b"bounded response");
    }

    #[test]
    fn no_content_response_uses_closed_json_wire_without_requiring_a_header() {
        let headers = reqwest::header::HeaderMap::new();
        assert_eq!(
            finalize_response(204, &headers, String::new()).unwrap(),
            DesktopApiResponse {
                status: 204,
                content_type: "application/json".to_owned(),
                body: String::new(),
            }
        );
        assert_eq!(
            finalize_response(204, &headers, "{}".to_owned()),
            Err("desktop_proxy_invalid_response".to_owned())
        );
    }
}
