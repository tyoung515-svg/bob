use bob_engine::functional::{Message, Session};
use bob_engine::workbench::StandaloneConfig;
use std::{
    io::{Read, Write},
    net::TcpListener,
};
fn server(response: String) -> (String, std::thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!(
        "http://{}/v1/chat/completions",
        listener.local_addr().unwrap()
    );
    let t = std::thread::spawn(move || {
        let (mut s, _) = listener.accept().unwrap();
        s.set_read_timeout(Some(std::time::Duration::from_secs(3)))
            .unwrap();
        let mut data = vec![];
        let mut b = [0; 8192];
        loop {
            let n = s.read(&mut b).unwrap();
            if n == 0 {
                break;
            }
            data.extend_from_slice(&b[..n]);
            if let Some(i) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                let h = String::from_utf8_lossy(&data[..i]).to_lowercase();
                let len = h
                    .lines()
                    .find_map(|l| l.strip_prefix("content-length: "))
                    .unwrap()
                    .parse::<usize>()
                    .unwrap();
                if data.len() >= i + 4 + len {
                    break;
                }
            }
        }
        let _ = s.write_all(response.as_bytes());
        String::from_utf8(data).unwrap()
    });
    (url, t)
}
#[test]
fn standalone_http_receives_native_project_context_without_bob_dependency() {
    let body=serde_json::json!({"model":"observed-standalone","id":"synthetic-context-http","choices":[{"message":{"content":r#"{"type":"message","content":"own endpoint context answer"}"#}}]}).to_string();
    let (url,t)=server(format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body));
    let config=StandaloneConfig::new(&url,"selected-standalone",None).unwrap();
    let root=std::env::temp_dir().join(format!("projects-standalone-{}",uuid::Uuid::new_v4()));
    let mut s=Session::create(&root).unwrap();let mut p=bob_engine::organizational::Projects::read(&root).unwrap();
    let group=p.create(&root,"Local only","No Bob API","STANDALONE-CONTEXT-ONLY-943").unwrap();s.move_to_group(Some(&group.id)).unwrap();
    s.turn("State current context",&mut |m|config.complete(m)).unwrap();
    let request=t.join().unwrap();assert!(request.contains("STANDALONE-CONTEXT-ONLY-943"));assert!(request.contains("selected-standalone"));
    assert_eq!(s.receipts[0]["transport"],"standalone_http");assert_eq!(s.receipts[0]["project_context"]["digest"],group.digest);assert!(s.project.is_none());std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn standalone_real_http_drives_existing_agent_loop_without_bob() {
    let body=serde_json::json!({"model":"observed","id":"local-test","choices":[{"message":{"content":r#"{"type":"message","content":"own backend answer"}"#}}]}).to_string();
    let (url, t) = server(format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    ));
    let config = StandaloneConfig::new(&url, "selected-model", None).unwrap();
    let mut session = Session::new();
    session.turn("hello", &mut |m| config.complete(m)).unwrap();
    assert_eq!(
        session.messages.last().unwrap().content,
        "own backend answer"
    );
    let request = t.join().unwrap();
    assert!(request.contains("selected-model"));
    assert!(!request.contains("Authorization:"));
    assert_eq!(session.receipts[0]["transport"], "standalone_http");
}
#[test]
fn standalone_does_not_follow_redirect_or_accept_oversize_or_raw_errors() {
    for response in [
        "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/stolen\r\nContent-Length: 0\r\n\r\n"
            .to_owned(),
        "HTTP/1.1 401 Unauthorized\r\nContent-Length: 11\r\n\r\nSECRET_BODY".to_owned(),
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: 200000\r\n\r\n{}",
            "x".repeat(200000)
        ),
    ] {
        let (url, t) = server(response);
        let c = StandaloneConfig::new(&url, "m", None).unwrap();
        let error = c
            .complete(&[Message {
                role: "user".into(),
                content: "hello".into(),
            }])
            .unwrap_err();
        assert!(!error.contains("SECRET_BODY"));
        t.join().unwrap();
    }
}

#[test]
fn credential_value_is_native_only_and_reflected_secret_is_rejected() {
    let token = "TEST_ONLY_NOT_A_REAL_KEY_746";
    let name = "BOB_STANDALONE_REFLECT_TEST_KEY";
    std::env::set_var(name, token);
    let body=serde_json::json!({"model":"fixture","choices":[{"message":{"content":format!("echoed {token}")}}]}).to_string();
    let (url, t) = server(format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{}",
        body.len(),
        body
    ));
    let c = StandaloneConfig::new(&url, "fixture", Some(name.into())).unwrap();
    let result = c.complete(&[Message {
        role: "user".into(),
        content: "hello".into(),
    }]);
    let request = t.join().unwrap();
    assert!(request.contains(&format!("Authorization: Bearer {token}")));
    assert!(
        result.is_err(),
        "successful response must not expose reflected credential to webview/history"
    );
    assert!(!result.unwrap_err().contains(token));
    assert!(!serde_json::to_string(&c).unwrap().contains(token));
    std::env::remove_var(name);
}
#[test]
fn standalone_settings_reject_untrusted_urls_and_secret_names() {
    for endpoint in [
        "file:///etc/passwd",
        "http://remote.example/v1/chat/completions",
        "https://u:p@example.com/v1",
        "http://127.0.0.1/x#frag",
        "https://example.com/x?key=secret",
    ] {
        assert!(
            StandaloneConfig::new(endpoint, "model", None).is_err(),
            "{endpoint}"
        );
    }
    for name in [
        "PATH",
        "BOB_AGENT_TOKEN",
        "BOB_STANDALONE_bad_KEY",
        "BOB_STANDALONE_KEY\n",
    ] {
        assert!(StandaloneConfig::new(
            "http://127.0.0.1:1234/v1/chat/completions",
            "m",
            Some(name.into())
        )
        .is_err());
    }
    assert!(StandaloneConfig::new(
        "https://api.example.test/v1/chat/completions",
        "user/model-1",
        Some("BOB_STANDALONE_API_KEY".into())
    )
    .is_ok());
    assert!(
        StandaloneConfig::new("http://[::1]:1234/v1/chat/completions", "bad\nmodel", None).is_err()
    );
}
