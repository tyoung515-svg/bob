//! Organizational metadata only: never a filesystem root, route, or authority ledger.
use serde::{Deserialize, Serialize};
use std::{
    io::{Read, Write},
    path::Path,
};
use ts_rs::TS;
use uuid::Uuid;
const FILE: &str = ".organizational-projects.json";
const LIMIT: u64 = 16 * 1024 * 1024;
#[derive(Debug, Clone, Serialize, Deserialize, TS)]
#[serde(deny_unknown_fields)]
pub struct OrganizationalProject {
    pub id: String,
    pub name: String,
    pub description: String,
    pub instructions: String,
    pub version: u32,
    pub digest: String,
    pub collapsed: bool,
}
impl OrganizationalProject {
    fn digest(&self) -> Result<String, String> {
        crate::digest::jcs_sha256(&serde_json::json!({"schema_version":"project-context-v1","id":self.id,"version":self.version,"name":self.name,"description":self.description,"instructions":self.instructions})).map_err(|e|e.to_string())
    }
    fn validate(&self) -> Result<(), String> {
        valid_id(&self.id)?;
        if self.version == 0
            || self.name.trim().is_empty()
            || self.name.len() > 160
            || self.name.chars().any(char::is_control)
            || self.description.len() > 2048
            || self.instructions.len() > 8192
            || self
                .description
                .chars()
                .chain(self.instructions.chars())
                .any(|c| c.is_control() && c != '\n' && c != '\t')
        {
            return Err("Project name: 1..160 bytes; description: 2048 bytes; instructions: 8192 bytes; no unsafe controls".into());
        }
        if self.digest() != Ok(self.digest.clone()) {
            return Err("Project context digest mismatch".into());
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Projects {
    pub schema_version: String,
    pub items: Vec<OrganizationalProject>,
}
impl Default for Projects {
    fn default() -> Self {
        Self {
            schema_version: "organizational-projects-v1".into(),
            items: vec![],
        }
    }
}
pub fn valid_id(id: &str) -> Result<(), String> {
    if Uuid::parse_str(id)
        .ok()
        .is_some_and(|u| u.to_string() == id)
    {
        Ok(())
    } else {
        Err("Invalid native organizational project identifier".into())
    }
}
impl Projects {
    pub fn read(root: &Path) -> Result<Self, String> {
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
        let root = crate::functional::private_root(root)?;
        let f = match std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(root.join(FILE))
        {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Self::default()),
            Err(_) => return Err("Project metadata inaccessible".into()),
        };
        let m = f.metadata().map_err(|e| e.to_string())?;
        if !m.is_file()
            || m.uid() != unsafe { libc::geteuid() }
            || m.nlink() != 1
            || m.mode() & 0o077 != 0
            || m.len() > LIMIT
        {
            return Err("Project metadata must be a bounded private regular file".into());
        }
        let mut bytes = vec![];
        f.take(LIMIT + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() as u64 > LIMIT {
            return Err("Project metadata oversized".into());
        }
        let s: Self = serde_json::from_slice(&bytes)
            .map_err(|_| "Invalid project metadata; no silent reset")?;
        if s.schema_version != "organizational-projects-v1" || s.items.len() > 512 {
            return Err("Unsupported project metadata version/limit".into());
        }
        let mut ids = std::collections::HashSet::new();
        for p in &s.items {
            p.validate()?;
            if !ids.insert(&p.id) {
                return Err("Duplicate project ID".into());
            }
        }
        Ok(s)
    }
    pub fn get(&self, id: &str) -> Result<&OrganizationalProject, String> {
        valid_id(id)?;
        self.items
            .iter()
            .find(|p| p.id == id)
            .ok_or_else(|| "Organizational project not found in this private store".into())
    }
    fn save(root: &Path, next: &Self) -> Result<(), String> {
        use std::os::unix::fs::OpenOptionsExt;
        let root = crate::functional::private_root(root)?;
        let bytes = serde_json::to_vec(next).map_err(|e| e.to_string())?;
        if bytes.len() as u64 > LIMIT {
            return Err("Project metadata storage limit".into());
        }
        let tmp = root.join(format!(".projects-{}.tmp", Uuid::new_v4()));
        let result = (|| {
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&tmp)
                .map_err(|e| e.to_string())?;
            f.write_all(&bytes).map_err(|e| e.to_string())?;
            f.sync_all().map_err(|e| e.to_string())?;
            std::fs::rename(&tmp, root.join(FILE)).map_err(|e| e.to_string())?;
            crate::sync_directory(&root).map_err(|e| e.to_string())
        })();
        let _ = std::fs::remove_file(tmp);
        result
    }
    pub fn create(
        &mut self,
        root: &Path,
        name: &str,
        description: &str,
        instructions: &str,
    ) -> Result<OrganizationalProject, String> {
        if self.items.len() >= 512 {
            return Err("Project count limit 512 reached".into());
        }
        let mut p = OrganizationalProject {
            id: Uuid::new_v4().to_string(),
            name: name.trim().into(),
            description: description.into(),
            instructions: instructions.into(),
            version: 1,
            digest: String::new(),
            collapsed: false,
        };
        p.digest = p.digest()?;
        p.validate()?;
        let mut next = self.clone();
        next.items.push(p.clone());
        Self::save(root, &next)?;
        *self = next;
        Ok(p)
    }
    pub fn edit(
        &mut self,
        root: &Path,
        id: &str,
        expected_version: u32,
        name: &str,
        description: &str,
        instructions: &str,
    ) -> Result<OrganizationalProject, String> {
        let mut p = self.get(id)?.clone();
        if p.version != expected_version {
            return Err("Stale project version; refresh before editing".into());
        }
        p.version = p
            .version
            .checked_add(1)
            .ok_or("Project version exhausted")?;
        p.name = name.trim().into();
        p.description = description.into();
        p.instructions = instructions.into();
        p.digest = p.digest()?;
        p.validate()?;
        let mut next = self.clone();
        *next.items.iter_mut().find(|v| v.id == id).unwrap() = p.clone();
        Self::save(root, &next)?;
        *self = next;
        Ok(p)
    }
    pub fn collapse(&mut self, root: &Path, id: &str, collapsed: bool) -> Result<(), String> {
        self.get(id)?;
        let mut next = self.clone();
        next.items
            .iter_mut()
            .find(|p| p.id == id)
            .unwrap()
            .collapsed = collapsed;
        Self::save(root, &next)?;
        *self = next;
        Ok(())
    }
}
