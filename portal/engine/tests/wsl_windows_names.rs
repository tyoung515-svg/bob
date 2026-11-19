#[cfg(target_os="linux")]
#[test]
fn wsl_refuses_windows_namespace_spellings_before_filesystem_access(){
 use bob_engine::functional_project::Project;
 let root=std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target").join(format!("bob-wsl-names-{}",uuid::Uuid::new_v4()));std::fs::create_dir_all(&root).unwrap();
 std::env::remove_var("BOB_WSL_ENGINE");let project=Project::attach(&root).unwrap();std::env::set_var("BOB_WSL_ENGINE","1");
 for path in ["CON","NUL","com1.txt","lpt9","AUX.txt","PRN","a:stream","trailing.","trailing ",r"a\..\b",r"C:\folder\file",r"\\?\C:\file",r"\\server\share\file"]{
  let e=project.before(path).unwrap_err();assert!(e.contains("Windows-shaped"),"{path}: {e}");
 }
 std::env::remove_var("BOB_WSL_ENGINE");
}
