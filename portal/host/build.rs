//! Compile the current native command implementations, not a second product.
//! Only Tauri's IPC attributes/import/State wrapper are adapted for stdio.
use quote::quote;
use std::{env,fs,path::PathBuf};
use syn::{Item,FnArg,Pat,Type};
fn camel(s:&str)->String { let mut upper=false;let mut out=String::new();for c in s.chars(){if c=='_'{upper=true;}else if upper{out.extend(c.to_uppercase());upper=false;}else{out.push(c)}}out }
fn main(){
 let out=PathBuf::from(env::var_os("OUT_DIR").unwrap());let mut arms=vec![];let mut names=vec![];
 for module in ["functional","workbench"]{
  let path=format!("../shell/src-tauri/src/{module}.rs");println!("cargo:rerun-if-changed={path}");
  let source=fs::read_to_string(path).unwrap();let mut file=syn::parse_file(&source).unwrap();assert!(file.attrs.iter().all(|a|a.path().is_ident("doc")));file.attrs.clear();
  file.items.retain(|item|!matches!(item,Item::Use(u) if quote!(#u).to_string()=="use tauri :: State ;"));
  for item in &mut file.items { if let Item::Fn(f)=item {
   let command=f.attrs.iter().any(|a|a.path().segments.first().is_some_and(|s|s.ident=="tauri"));
   f.attrs.retain(|a|a.path().segments.first().is_none_or(|s|s.ident!="tauri"));
   if !command{continue}let ident=&f.sig.ident;let command_name=ident.to_string();names.push(command_name.clone());let mod_ident=syn::Ident::new(module,ident.span());let mut values=vec![];
   for arg in &mut f.sig.inputs{if let FnArg::Typed(p)=arg{
    if let Type::Path(t)=&*p.ty{if t.path.segments.first().is_some_and(|s|s.ident=="State") {p.ty=Box::new(syn::parse_quote!(&crate::functional::AppState));values.push(quote!(state));continue;}}
    let Pat::Ident(pat)=&*p.pat else{panic!("unsupported command argument")};let name=camel(&pat.ident.to_string());let ty=&p.ty;
    values.push(quote!(serde_json::from_value::<#ty>(args.get(#name).cloned().unwrap_or(serde_json::Value::Null)).map_err(|e|format!("invalid {}: {}",#name,e))?));
   }}
   let call=quote!(#mod_ident::#ident(#(#values),*));
   let value=if command_name=="app_developer_mode"{quote!(serde_json::to_value(#call))}else{quote!(serde_json::to_value(#call?))};
   arms.push(quote!(#command_name=>#value.map_err(|e|e.to_string()),));
  }}
  fs::write(out.join(format!("{module}.rs")),quote!(#file).to_string()).unwrap();
 }
 assert_eq!(names.len(),25,"current command inventory changed: review adapter");
 println!("cargo:rerun-if-changed=commands.json");
 assert_eq!(fs::read_to_string("commands.json").expect("reviewed command inventory missing").trim(),serde_names(&names),"current native command inventory drift; regenerate and review Windows adapter");
 fs::write(out.join("dispatch.rs"),quote!(fn dispatch(state:&functional::AppState,command:&str,args:&serde_json::Value)->Result<serde_json::Value,String>{match command{#(#arms)*_=>Err("unknown command; no action attempted".into())}}).to_string()).unwrap();
 fs::write(out.join("commands.json"),serde_names(&names)).unwrap();
}
fn serde_names(names:&[String])->String{format!("[{}]",names.iter().map(|n|format!("\"{n}\"")).collect::<Vec<_>>().join(","))}
