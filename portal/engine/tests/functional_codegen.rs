#[test]
fn functional_types_are_generated_from_rust_and_not_hand_mirrored() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../shell/src/functional-protocol.ts");
    let actual = std::fs::read_to_string(path).unwrap_or_default();
    assert!(
        actual.starts_with("// GENERATED"),
        "successor types must be generated"
    );
    assert_eq!(
        actual,
        bob_engine::functional::bindings(),
        "successor protocol drift: regenerate, never hand edit"
    );
}
