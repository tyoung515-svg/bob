#[test]
fn functional_codegen_has_no_trailing_whitespace() {
    let generated=bob_engine::functional::bindings();
    assert!(generated.lines().all(|line|line==line.trim_end()),"generated native contract must be clean for logical commits");
}
