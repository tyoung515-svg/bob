use super::*;

#[test]
fn network_commands_have_closed_schemas_and_scoped_owner_resources() {
    for (command, raw, op, write) in [
        ("portal_builder_network_set_v2", json!({"contract_version":2,"project_id":"p","add":["example.com"],"expected_revision":"9007199254740993","request_key":"k"}), "builder.network.set", true),
        ("portal_builder_network_log_v2", json!({"contract_version":2,"conversation_id":"c"}), "builder.network_log", false),
        ("portal_builder_network_log_v2", json!({"contract_version":2,"job_id":"j"}), "builder.network_log", false),
    ] {
        assert!(PORTAL_COMMANDS.contains(&command));
        let (actual, _, args) = normalize(command, raw.clone()).unwrap();
        assert_eq!(actual, op); assert_eq!(is_write_op(op), write);
        if write { assert_eq!(args["expected_revision"], "9007199254740993"); }
        let mut bad = raw.clone(); bad["owner"] = json!(true); assert!(normalize(command, bad).is_err());
        let mut bad = raw; bad["contract_version"] = json!(true); assert!(normalize(command, bad).is_err());
    }
    assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"builder.network.set","resource":{"project_id":"p"}})).is_ok());
    for resource in [json!({}), json!({"conversation_id":"c"}), json!({"project_id":"p","conversation_id":"c"})] {
        assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"builder.network.set","resource":resource})).is_err());
    }
    assert!(normalize("portal_builder_approve_v2", json!({"contract_version":2,"approval_id":"a","decision":"allow_project","request_key":"k"})).is_ok());
}

#[test]
fn network_domain_changes_reject_malformed_and_ambiguous_input() {
    let good = json!({"contract_version":2,"project_id":"p","add":["docs.example.com"],"expected_revision":0,"request_key":"k"});
    for field in ["project_id", "expected_revision", "request_key"] {
        let mut bad = good.clone(); bad.as_object_mut().unwrap().remove(field);
        assert!(normalize("portal_builder_network_set_v2", bad).is_err());
    }
    for host in ["https://example.com", "example.com:443", "*.example.com", "EXAMPLE.com", "a..com", "-a.com", "a-.com", "example.com.", "localhost", "127.0.0.1", "[::1]", "a/b.com", "a.com\n", "é.com", "a.123"] {
        let mut bad = good.clone(); bad["add"] = json!([host]);
        assert!(normalize("portal_builder_network_set_v2", bad).is_err(), "{host}");
    }
    for changes in [json!({"add":null}), json!({"add":"x.com"}), json!({"add":[]}), json!({"add":[true]}), json!({"add":["a.com","a.com"]}), json!({"add":["a.com"],"remove":["a.com"]}), json!({"add":(0..257).map(|i| format!("x{i}.com")).collect::<Vec<_>>()})] {
        let mut bad = good.clone();
        for (k,v) in changes.as_object().unwrap() { bad[k] = v.clone(); }
        assert!(normalize("portal_builder_network_set_v2", bad).is_err());
    }
    let mut remove = good.clone(); remove.as_object_mut().unwrap().remove("add"); remove["remove"] = json!(["a.com"]);
    assert!(normalize("portal_builder_network_set_v2", remove).is_ok());
    for revision in [json!(-1), json!(true), json!(0.5), json!(9007199254740992_u64), json!("01"), json!("9223372036854775808")] {
        let mut bad = good.clone(); bad["expected_revision"] = revision;
        assert!(normalize("portal_builder_network_set_v2", bad).is_err());
    }
    for selectors in [json!({}), json!({"conversation_id":"c","job_id":"j"}), json!({"job_id":null}), json!({"job_id":" bad "})] {
        let mut bad = selectors; bad["contract_version"] = json!(2);
        assert!(normalize("portal_builder_network_log_v2", bad).is_err());
    }
}
