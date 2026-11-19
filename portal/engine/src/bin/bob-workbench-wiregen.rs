fn main() {
    let path =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../shell/src/workbench-protocol.ts");
    let generated = bob_engine::workbench::bindings();
    if std::env::args().any(|a| a == "--check") {
        assert_eq!(
            std::fs::read_to_string(path).unwrap(),
            generated,
            "workbench protocol drift"
        );
    } else {
        std::fs::write(path, generated).unwrap();
    }
}
