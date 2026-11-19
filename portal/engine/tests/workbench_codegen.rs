#[test]
fn generated_workbench_contract_has_no_drift() {
    let generated = bob_engine::workbench::bindings();
    assert!(generated.contains("export type WorkbenchSnapshot"));
    assert!(generated.contains("import type { AppSnapshot }"));
    let path =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../shell/src/workbench-protocol.ts");
    assert_eq!(std::fs::read_to_string(path).unwrap_or_default(), generated);
}
