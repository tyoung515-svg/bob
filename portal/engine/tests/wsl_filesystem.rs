//! WSL filesystem contract; run explicitly, not silently skipped in native tiers.
use bob_engine::functional_project::Project;
#[test]
fn wsl_engine_rejects_non_ext4_even_when_directory_looks_normal(){
 let base=std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/wsl-negative-projects");
 std::fs::create_dir_all(&base).unwrap();
 let root=base.join(format!("bob-wsl-fs-{}",uuid::Uuid::new_v4()));
 std::fs::create_dir(&root).unwrap();
 // This explicit test is run on the server's btrfs and on Windows drvfs in WSL.
 use std::os::fd::AsRawFd;
 let file=std::fs::File::open(&root).unwrap();let mut fs=std::mem::MaybeUninit::<libc::statfs>::uninit();
 assert_eq!(unsafe{libc::fstatfs(file.as_raw_fd(),fs.as_mut_ptr())},0);
 assert_ne!(unsafe{fs.assume_init()}.f_type,0xef53,"test requires a non-ext4 temporary root");
 std::env::set_var("BOB_WSL_ENGINE","1");
 let result=Project::attach(&root);
 std::env::remove_var("BOB_WSL_ENGINE");
 assert!(result.is_err(),"WSL host accepted a non-ext4 project; drvfs reparse safety is unproven");
}
