//! Native process-private CNG key. Only its public verifier is enrolled in WSL.
use serde_json::Value;
use sha2::{Digest, Sha256};
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
pub fn digest(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}
pub struct Signer {
    #[cfg(windows)]
    algorithm: usize,
    #[cfg(windows)]
    key: usize,
    pub public_blob: Vec<u8>,
}
impl Signer {
    pub fn new() -> Result<Self, String> {
        #[cfg(windows)]
        {
            use windows_sys::Win32::Security::Cryptography::*;
            let mut alg = std::ptr::null_mut();
            let mut key = std::ptr::null_mut();
            unsafe {
                if BCryptOpenAlgorithmProvider(&mut alg, BCRYPT_RSA_ALGORITHM, std::ptr::null(), 0)
                    < 0
                {
                    return Err("Native signing provider unavailable".into());
                }
                if BCryptGenerateKeyPair(alg, &mut key, 2048, 0) < 0
                    || BCryptFinalizeKeyPair(key, 0) < 0
                {
                    if !key.is_null() {
                        BCryptDestroyKey(key);
                    }
                    BCryptCloseAlgorithmProvider(alg, 0);
                    return Err("Native receipt key unavailable".into());
                }
                let mut count = 0;
                let mut blob = vec![0u8; 1024];
                if BCryptExportKey(
                    key,
                    std::ptr::null_mut(),
                    BCRYPT_RSAPUBLIC_BLOB,
                    blob.as_mut_ptr(),
                    blob.len() as u32,
                    &mut count,
                    0,
                ) < 0
                {
                    BCryptDestroyKey(key);
                    BCryptCloseAlgorithmProvider(alg, 0);
                    return Err("Public receipt verifier unavailable".into());
                }
                blob.truncate(count as usize);
                Ok(Self {
                    algorithm: alg as usize,
                    key: key as usize,
                    public_blob: blob,
                })
            }
        }
        #[cfg(not(windows))]
        {
            Err("PC actions require Windows".into())
        }
    }
    pub fn sign(&self, payload: &Value) -> Result<String, String> {
        #[cfg(windows)]
        {
            use windows_sys::Win32::Security::Cryptography::*;
            let input =
                Sha256::digest(serde_json::to_vec(payload).map_err(|_| "Invalid native receipt")?);
            let info = BCRYPT_PKCS1_PADDING_INFO {
                pszAlgId: BCRYPT_SHA256_ALGORITHM,
            };
            let mut output = vec![0u8; 256];
            let mut count = 0;
            if unsafe {
                BCryptSignHash(
                    self.key as _,
                    &info as *const _ as _,
                    input.as_ptr(),
                    input.len() as u32,
                    output.as_mut_ptr(),
                    output.len() as u32,
                    &mut count,
                    BCRYPT_PAD_PKCS1,
                )
            } < 0
            {
                return Err("Cannot sign native receipt".into());
            }
            output.truncate(count as usize);
            Ok(hex(&output))
        }
        #[cfg(not(windows))]
        {
            let _ = payload;
            Err("PC actions require Windows".into())
        }
    }
}
impl Drop for Signer {
    fn drop(&mut self) {
        #[cfg(windows)]
        unsafe {
            use windows_sys::Win32::Security::Cryptography::*;
            BCryptDestroyKey(self.key as _);
            BCryptCloseAlgorithmProvider(self.algorithm as _, 0);
        }
    }
}

// Fixed root command, stdin contains PUBLIC bytes only. Core/broker cannot enroll.
const ENROLL: &str = r#"import json,os,pathlib,sys,uuid
v=json.loads(sys.stdin.read(8193));b=bytes.fromhex(v['public_blob'])
if not 280<=len(b)<=300: raise ValueError('invalid public key')
p=pathlib.Path('/etc/bob/pc-host-public.json')
parent=p.parent.lstat()
if not p.parent.is_dir() or p.parent.is_symlink() or parent.st_uid or parent.st_mode&0o022: raise ValueError('unsafe public verifier directory')
t=p.with_name('.pc-public-'+uuid.uuid4().hex+'.new')
try:
 fd=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
 with os.fdopen(fd,'w') as f: json.dump(v,f);f.flush();os.fsync(f.fileno())
 os.replace(t,p)
 directory=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
 try: os.fsync(directory)
 finally: os.close(directory)
finally: t.unlink(missing_ok=True)
"#;
pub fn enroll(signer: &Signer) -> Result<(), String> {
    #[cfg(windows)]
    {
        let value = serde_json::json!({"public_blob":hex(&signer.public_blob)}).to_string();
        let args = [
            "-d",
            crate::self_repair::distro(),
            "-u",
            "root",
            "--exec",
            "/opt/bob/.venv/bin/python",
            "-I",
            "-c",
            ENROLL,
        ]
        .map(String::from)
        .to_vec();
        let result =
            crate::runtime::platform::wsl(&args, Some(value), std::time::Duration::from_secs(20))?;
        if result.0 {
            Ok(())
        } else {
            Err("Cannot enroll the native public receipt verifier".into())
        }
    }
    #[cfg(not(windows))]
    {
        let _ = signer;
        Err("PC actions require Windows".into())
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    #[test]
    fn native_cng_receipts_verify_and_tampering_fails() {
        use windows_sys::Win32::Security::Cryptography::*;
        let signer = Signer::new().unwrap();
        let tick = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let payload = serde_json::json!({"kind":"pc.claim","host_id":digest(&signer.public_blob),"iat":tick,"exp":tick+60,"nonce":"a".repeat(32),"task_id":"b".repeat(32),"request_id":"c".repeat(32),"args_sha256":"d".repeat(64)});
        let signature = signer.sign(&payload).unwrap();
        let bytes = (0..signature.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&signature[i..i + 2], 16).unwrap())
            .collect::<Vec<_>>();
        let hash = Sha256::digest(serde_json::to_vec(&payload).unwrap());
        let info = BCRYPT_PKCS1_PADDING_INFO {
            pszAlgId: BCRYPT_SHA256_ALGORITHM,
        };
        assert!(
            unsafe {
                BCryptVerifySignature(
                    signer.key as _,
                    &info as *const _ as _,
                    hash.as_ptr(),
                    32,
                    bytes.as_ptr(),
                    bytes.len() as u32,
                    BCRYPT_PAD_PKCS1,
                )
            } >= 0
        );
        let mut tampered = payload.clone();
        tampered["task_id"] = serde_json::json!("e".repeat(32));
        let bad = Sha256::digest(serde_json::to_vec(&tampered).unwrap());
        assert!(
            unsafe {
                BCryptVerifySignature(
                    signer.key as _,
                    &info as *const _ as _,
                    bad.as_ptr(),
                    32,
                    bytes.as_ptr(),
                    bytes.len() as u32,
                    BCRYPT_PAD_PKCS1,
                )
            } < 0
        );
        if let Ok(path) = std::env::var("BOB_PC_SIGNATURE_FIXTURE") {
            std::fs::write(path,serde_json::to_vec_pretty(&serde_json::json!({"public_blob":hex(&signer.public_blob),"payload":payload,"signature":signature})).unwrap()).unwrap();
        }
    }
    use std::time::{SystemTime, UNIX_EPOCH};
}
