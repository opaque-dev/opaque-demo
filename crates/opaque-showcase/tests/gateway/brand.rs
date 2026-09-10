use super::*;

#[path = "../../../../assets/brand/embedded.rs"]
mod shared_brand;

#[tokio::test]
async fn brand_assets_are_exact_public_allowlisted_bytes_under_existing_security() {
    let fixture = Fixture::new(false).await;
    for asset in shared_brand::ASSETS {
        let response = fixture
            .router()
            .oneshot(fixture.request("GET", &format!("/brand/{}", asset.path), Value::Null))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK, "{}", asset.path);
        assert_eq!(response.headers()[header::CONTENT_TYPE], asset.content_type);
        assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
        assert_eq!(
            response.headers()[header::X_CONTENT_TYPE_OPTIONS],
            "nosniff"
        );
        let csp = response.headers()[header::CONTENT_SECURITY_POLICY]
            .to_str()
            .unwrap();
        assert!(csp.contains("font-src 'self'"));
        assert!(csp.contains("default-src 'none'"));
        assert!(csp.contains("script-src 'sha256-"));
        assert!(csp.contains("frame-ancestors 'none'"));
        assert_eq!(
            to_bytes(response.into_body(), 16 * 1024 * 1024)
                .await
                .unwrap()
                .as_ref(),
            asset.bytes
        );
    }
    for path in [
        "README.md",
        "manifest.json",
        "provenance.json",
        "embedded.rs",
        "../Cargo.toml",
        "fonts/unknown.ttf",
    ] {
        assert!(shared_brand::get(path).is_none());
        let response = fixture
            .router()
            .oneshot(fixture.request("GET", &format!("/brand/{path}"), Value::Null))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
    }
    for (header, value) in [
        (header::HOST, "untrusted.example"),
        (header::ORIGIN, "https://untrusted.example"),
    ] {
        let mut request = fixture.request("GET", "/brand/opaque.css", Value::Null);
        request.headers_mut().insert(header, value.parse().unwrap());
        assert_eq!(
            fixture.router().oneshot(request).await.unwrap().status(),
            StatusCode::FORBIDDEN
        );
    }
    let response = fixture
        .router()
        .oneshot(fixture.request("GET", "/api/session", Value::Null))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
