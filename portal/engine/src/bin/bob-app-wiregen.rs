fn main() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../shell/src/functional-protocol.ts");
    let generated = bob_engine::functional::bindings();
    if std::env::args().any(|a| a == "--check") {
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            generated,
            "functional protocol drift"
        )
    } else {
        std::fs::write(path, generated).unwrap();
    }
}
