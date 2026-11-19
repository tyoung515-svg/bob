use bob_engine::functional::{Session,Message};
use bob_engine::attachments::Upload;
#[test]
fn selected_text_is_real_untrusted_context_with_digest_and_no_project_grant(){
 let root=std::env::temp_dir().join(format!("attachment-test-{}",uuid::Uuid::new_v4()));let mut s=Session::create(&root).unwrap();
 let file=Upload{name:"facts.txt".into(),mime:"text/plain".into(),bytes:b"blind synthetic copper finch 319".to_vec()};
 s.turn_with_attachments("Read the supplied note",vec![file],&mut |m:&[Message]|{assert!(m.iter().any(|m|m.content.contains("blind synthetic copper finch 319")&&m.content.contains("UNTRUSTED")));Ok(serde_json::json!({"ok":true,"content":r#"{"type":"message","content":"synthetic answer"}"#}))}).unwrap();
 assert!(s.project.is_none());let e=s.events.iter().find(|e|e.kind=="attachment.context").unwrap();assert_eq!(e.data["attachments"][0]["digest"],bob_engine::sha256_digest(b"blind synthetic copper finch 319"));s.validate().unwrap();
 let before=std::fs::read_dir(&root).unwrap().count();
 for (name,mime,bytes) in [("../x","text/plain",b"x".to_vec()),("x.svg","image/svg+xml",b"<svg/>".to_vec()),("x.txt","text/plain",vec![0,255]),("x.png","image/png",b"not png".to_vec()),("x.txt","text/plain",vec![b'a';32769])]{assert!(s.turn_with_attachments("no",vec![Upload{name:name.into(),mime:mime.into(),bytes}],&mut |_|panic!("invalid attachment never dispatches")).is_err());}
 assert_eq!(before,std::fs::read_dir(&root).unwrap().count());std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn immutable_blob_reuse_never_follows_or_overwrites_symlink_or_special_file(){
 use std::os::unix::fs::{symlink,PermissionsExt};
 let root=std::env::temp_dir().join(format!("attachment-security-{}",uuid::Uuid::new_v4()));let mut s=Session::create(&root).unwrap();
 let bytes=b"unchanged selected text";let digest=bob_engine::sha256_digest(bytes);let blob=root.join(format!(".attachment-{}",digest.trim_start_matches("sha256:")));
 let file=||Upload{name:"note.md".into(),mime:"text/markdown".into(),bytes:bytes.to_vec()};
 let mut transport=|_:&[Message]|Ok(serde_json::json!({"ok":true,"content":r#"{"type":"message","content":"done"}"#}));
 s.turn_with_attachments("read",vec![file()],&mut transport).unwrap();let original=std::fs::read(&blob).unwrap();
 s.turn_with_attachments("again",vec![file()],&mut transport).unwrap();assert_eq!(original,std::fs::read(&blob).unwrap());
 assert_eq!(std::fs::metadata(&blob).unwrap().permissions().mode()&0o777,0o400);
 std::fs::remove_file(&blob).unwrap();let outside=root.join("outside");std::fs::write(&outside,"untouched").unwrap();symlink(&outside,&blob).unwrap();
 assert!(s.turn_with_attachments("no",vec![file()],&mut |_|panic!("symlink cannot dispatch")).is_err());assert_eq!(std::fs::read_to_string(&outside).unwrap(),"untouched");
 std::fs::remove_file(&blob).unwrap();std::fs::write(&blob,"different bytes").unwrap();std::fs::set_permissions(&blob,std::fs::Permissions::from_mode(0o400)).unwrap();
 assert!(s.turn_with_attachments("no",vec![file()],&mut |_|panic!("digest mismatch cannot dispatch")).is_err());assert_eq!(std::fs::read_to_string(&blob).unwrap(),"different bytes");
 assert!(s.turn_with_attachments("no",vec![file(),file(),file(),file(),file()],&mut |_|panic!("count cannot dispatch")).is_err());
 std::fs::remove_dir_all(root).unwrap();
}
