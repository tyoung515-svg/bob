use bob_engine::functional_project::Project;
use std::fs;
fn fixture() -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!("bob-functional-project-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&p).unwrap();
    p
}
#[test]
fn selected_project_lists_reads_and_searches_real_bytes() {
    let p = fixture();
    fs::write(p.join("notes.txt"), "fresh mango evidence 927\n").unwrap();
    let project = Project::attach(&p).unwrap();
    assert_eq!(project.list().unwrap(), vec!["notes.txt"]);
    assert_eq!(
        project.read("notes.txt").unwrap(),
        "fresh mango evidence 927\n"
    );
    assert!(project.search("mango").unwrap().to_string().contains("927"));
    fs::remove_dir_all(p).unwrap();
}

#[test]
fn file_update_and_creation_recheck_old_bytes() {
    let p = fixture();
    fs::write(p.join("note.txt"), "old").unwrap();
    let project = Project::attach(&p).unwrap();
    project.write("note.txt", Some("old"), "new").unwrap();
    assert_eq!(fs::read_to_string(p.join("note.txt")).unwrap(), "new");
    assert!(project.write("note.txt", Some("old"), "bad").is_err());
    assert_eq!(project.read("note.txt").unwrap(), "new");
    project.write("created.txt", None, "fresh").unwrap();
    assert!(project.write("created.txt", None, "replace").is_err());
    fs::remove_dir_all(p).unwrap();
}

#[test]
fn text_search_reports_unreadable_binary_files_without_hiding_matches() {
    let p = fixture();
    fs::write(p.join("notes.txt"), "mango evidence").unwrap();
    fs::write(p.join("image.bin"), [255, 254, 0]).unwrap();
    let project = Project::attach(&p).unwrap();
    let result = project.search("mango").unwrap();
    assert!(result["matches"].to_string().contains("mango evidence"));
    assert!(result["skipped"].to_string().contains("image.bin"));
    fs::remove_dir_all(p).unwrap();
}
