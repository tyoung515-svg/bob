use super::*;
use serde_json::json;

#[test]
fn workspace_browse_is_read_only_and_validates_the_ipc_boundary() {
    for path in [None, Some(""), Some("src/components")] {
        let mut args = json!({"contract_version":2,"project_id":"project"});
        if let Some(value) = path { args["path"] = json!(value); }
        let (op, long, payload) = normalize("portal_workspace_browse_v2",args).unwrap();
        assert_eq!(op,"workspace.browse");
        assert!(!long);
        assert_eq!(payload["project_id"],"project");
        assert!(payload.get("contract_version").is_none());
    }
    for path in ["..","../host","/absolute","C:/host","a\\b","a//b","a/../b"] {
        assert!(normalize("portal_workspace_browse_v2",json!({"contract_version":2,"project_id":"p","path":path})).is_err());
    }
    for args in [json!({"contract_version":2}), json!({"contract_version":2,"project_id":"p","owner":"forged"}),
                 json!({"contract_version":2,"project_id":"p","path":null})] {
        assert!(normalize("portal_workspace_browse_v2",args).is_err());
    }
}
