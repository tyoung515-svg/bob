//! Windows-only trusted surfaces. No shell, clipboard, plaintext backups or
//! app spool files. Native windows are capture-excluded before becoming visible.
use super::*;
use std::{os::windows::{ffi::OsStrExt, io::{FromRawHandle, OwnedHandle, AsRawHandle}}, ptr::{null, null_mut}, sync::Once};
use windows_sys::Win32::{Foundation::*, Security::{*, Authorization::*},
    Storage::{FileSystem::*, Xps::*}, Graphics::Gdi::*, System::{LibraryLoader::GetModuleHandleW, ErrorReporting::*, SystemServices::FILE_PERSISTENT_ACLS, Threading::{GetCurrentProcess,OpenProcessToken}},
    UI::{WindowsAndMessaging::*, Controls::Dialogs::*, Input::KeyboardAndMouse::SetFocus}};
fn wide(s:&str)->Vec<u16>{s.encode_utf16().chain(Some(0)).collect()}
fn path_wide(p:&Path)->Vec<u16>{p.as_os_str().encode_wide().chain(Some(0)).collect()}
fn fixed()->String{"native_recovery_unavailable".into()}
struct Dialog { edit: HWND, accepted: bool, text: String, message: Vec<u16> }
impl Drop for Dialog{fn drop(&mut self){wipe(&mut self.text);for b in &mut self.message{unsafe{std::ptr::write_volatile(b,0);}}}}
unsafe extern "system" fn edit_proc(h:HWND,m:u32,w:WPARAM,l:LPARAM)->LRESULT{
    // No clipboard operations, drag/drop, context menus or password reveal.
    if matches!(m,WM_COPY|WM_CUT|WM_PASTE|WM_CONTEXTMENU|WM_DROPFILES|WM_GETOBJECT|WM_PRINT|WM_PRINTCLIENT){return 0;}
    let old=GetWindowLongPtrW(h,GWLP_USERDATA);
    CallWindowProcW(std::mem::transmute::<isize,WNDPROC>(old),h,m,w,l)
}
unsafe extern "system" fn window_proc(h:HWND,m:u32,w:WPARAM,l:LPARAM)->LRESULT{
    let data=GetWindowLongPtrW(h,GWLP_USERDATA) as *mut Dialog;
    // No control/window text contains provisioning material. UIA/MSAA and
    // WM_PRINT snapshots receive no painted secret text.
    if matches!(m,WM_GETOBJECT|WM_PRINT|WM_PRINTCLIENT){return 0;}
    if !data.is_null() && m==WM_PAINT{
        let mut paint:PAINTSTRUCT=std::mem::zeroed();let dc=BeginPaint(h,&mut paint);
        let mut rect=RECT{left:20,top:20,right:670,bottom:225};
        DrawTextW(dc,(*data).message.as_mut_ptr(),((*data).message.len()-1) as i32,&mut rect,DT_LEFT|DT_WORDBREAK|DT_NOPREFIX);
        EndPaint(h,&paint);return 0;
    }
    if !data.is_null() && (m==WM_CLOSE || (m==WM_COMMAND && matches!(w & 0xffff,1|2))) {
        if m==WM_COMMAND && w & 0xffff==1 {
            (*data).accepted=true;
            if !(*data).edit.is_null(){
                let n=GetWindowTextLengthW((*data).edit).min(4096);
                let mut buf=vec![0u16;n as usize+1];GetWindowTextW((*data).edit,buf.as_mut_ptr(),buf.len() as i32);
                (*data).text=String::from_utf16_lossy(&buf[..n as usize]);
                for b in &mut buf{std::ptr::write_volatile(b,0);}
            }
        }
        if !(*data).edit.is_null(){SetWindowTextW((*data).edit,wide("").as_ptr());}
        DestroyWindow(h);return 0;
    }
    DefWindowProcW(h,m,w,l)
}
pub fn protect()->Result<(),String>{unsafe{
    if WerSetFlags(WER_FAULT_REPORTING_FLAG_NOHEAP|WER_FAULT_REPORTING_FLAG_NO_HEAP_ON_QUEUE|WER_FAULT_REPORTING_DISABLE_SNAPSHOT_CRASH|WER_FAULT_REPORTING_DISABLE_SNAPSHOT_HANG)<0{return Err(fixed());}Ok(())
}}
fn dialog(message:&str,input:bool,secret:bool)->Result<String,String>{
    unsafe{
        // Disable heap/snapshot inclusion in Windows Error Reporting. No custom
        // crash/analytics attachment is made by this module.
        protect()?;
        static REGISTER:Once=Once::new();
        let class=wide("BobTrustedRecoveryV1");let instance=GetModuleHandleW(null());
        REGISTER.call_once(||{let mut c:WNDCLASSW=std::mem::zeroed();c.lpfnWndProc=Some(window_proc);c.hInstance=instance;c.lpszClassName=class.as_ptr();c.hbrBackground=(COLOR_WINDOW+1) as HBRUSH;RegisterClassW(&c);});
        let h=CreateWindowExW(WS_EX_DLGMODALFRAME|WS_EX_TOPMOST,class.as_ptr(),wide("Bob - Confirm it's you").as_ptr(),WS_CAPTION|WS_SYSMENU,180,180,700,400,null_mut(),null_mut(),instance,null());
        if h.is_null(){return Err(fixed());}
        let mut affinity=0;
        if SetWindowDisplayAffinity(h,WDA_EXCLUDEFROMCAPTURE)==0||GetWindowDisplayAffinity(h,&mut affinity)==0||affinity!=WDA_EXCLUDEFROMCAPTURE{DestroyWindow(h);return Err(fixed());}
        let mut data=Box::new(Dialog{edit:null_mut(),accepted:false,text:String::new(),message:wide(message)});
        SetWindowLongPtrW(h,GWLP_USERDATA,(&mut *data as *mut Dialog) as isize);
        if input{
            data.edit=CreateWindowExW(WS_EX_CLIENTEDGE,wide("EDIT").as_ptr(),wide("").as_ptr(),WS_CHILD|WS_VISIBLE|WS_TABSTOP|ES_AUTOHSCROLL as u32|if secret{ES_PASSWORD as u32}else{0},20,230,650,32,h,10usize as HMENU,instance,null());
            SendMessageW(data.edit,0x00c5,4096,0); // EM_SETLIMITTEXT
            let old=SetWindowLongPtrW(data.edit,GWLP_WNDPROC,edit_proc as *const () as isize);if old==0{DestroyWindow(h);return Err(fixed());}SetWindowLongPtrW(data.edit,GWLP_USERDATA,old);
        }
        let ok=CreateWindowExW(0,wide("BUTTON").as_ptr(),wide("Continue").as_ptr(),WS_CHILD|WS_VISIBLE|WS_TABSTOP|BS_DEFPUSHBUTTON as u32,440,290,110,32,h,1usize as HMENU,instance,null());
        let cancel=CreateWindowExW(0,wide("BUTTON").as_ptr(),wide("Cancel").as_ptr(),WS_CHILD|WS_VISIBLE|WS_TABSTOP,560,290,110,32,h,2usize as HMENU,instance,null());
        if ok.is_null()||cancel.is_null()||(input&&data.edit.is_null()){DestroyWindow(h);return Err(fixed());}
        ShowWindow(h,SW_SHOW);SetForegroundWindow(h);if input{SetFocus(data.edit);}
        let mut msg:MSG=std::mem::zeroed();
        while IsWindow(h)!=0{let n=GetMessageW(&mut msg,null_mut(),0,0);if n<=0{DestroyWindow(h);break;}if IsDialogMessageW(h,&msg)==0{TranslateMessage(&msg);DispatchMessageW(&msg);}}
        for b in &mut data.message{std::ptr::write_volatile(b,0);}
        if !data.accepted{wipe(&mut data.text);return Err("native_cancelled".into());}Ok(std::mem::take(&mut data.text))
    }
}
pub fn entry(label:&str,secret:bool)->Result<String,String>{
    let s=dialog(label,true,secret)?;
    if s.is_empty()||s.len()>LIMIT as usize{return Err(fail());}Ok(s)
}
pub fn display_seed(seed:&str)->Result<(),String>{
    let mut text=format!("Authenticator setup\nEnter this seed in your authenticator app:\n{}\n\nKeep it private. After saving your recovery material, enter a code from the new authenticator to activate it.",seed);
    let result=dialog(&text,false,false).map(|_|());wipe(&mut text);result
}
struct Volume { mount:String, guid:String, serial:u32, _root:OwnedHandle }
fn info(mount:&str)->Result<(String,u32),String>{unsafe{
    let w=wide(mount);if GetDriveTypeW(w.as_ptr())!=2 /* DRIVE_REMOVABLE */{return Err("removable_volume_required".into());}
    let mut guid=[0u16;128];let mut serial=0;
    if GetVolumeNameForVolumeMountPointW(w.as_ptr(),guid.as_mut_ptr(),128)==0||GetVolumeInformationW(w.as_ptr(),null_mut(),0,&mut serial,null_mut(),null_mut(),null_mut(),0)==0{return Err("volume_unavailable".into());}
    let end=guid.iter().position(|x|*x==0).ok_or_else(fixed)?;Ok((String::from_utf16_lossy(&guid[..end]),serial))
}}
impl Volume {
    fn pin(mount:String)->Result<Self,String>{
        let (guid,serial)=info(&mount)?;
        let w=wide(&guid);
        let h=unsafe{CreateFileW(w.as_ptr(),0,FILE_SHARE_READ|FILE_SHARE_WRITE,null(),OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,null_mut())};
        if h==INVALID_HANDLE_VALUE{return Err("volume_unavailable".into());}
        let v=Self{mount,guid,serial,_root:unsafe{OwnedHandle::from_raw_handle(h)}};v.validate()?;Ok(v)
    }
    fn validate(&self)->Result<(),String>{
        let (guid,serial)=info(&self.mount)?;if guid!=self.guid||serial!=self.serial{return Err("volume_changed".into());}
        let mut meta:BY_HANDLE_FILE_INFORMATION=unsafe{std::mem::zeroed()};
        if unsafe{GetFileInformationByHandle(self._root.as_raw_handle(),&mut meta)}==0||meta.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT!=0||meta.dwVolumeSerialNumber!=serial{return Err("volume_changed".into());}Ok(())
    }
}
fn pick_volume()->Result<Volume,String>{unsafe{
    let mask=GetLogicalDrives();let mut choices=Vec::new();
    for i in 0..26{if mask&(1<<i)!=0{let mount=format!("{}:\\\\",(b'A'+i as u8) as char).replace("\\\\","\\");if let Ok((_,_))=info(&mount){let mut label=[0u16;256];GetVolumeInformationW(wide(&mount).as_ptr(),label.as_mut_ptr(),256,null_mut(),null_mut(),null_mut(),null_mut(),0);let n=label.iter().position(|x|*x==0).unwrap_or(0);choices.push((mount,String::from_utf16_lossy(&label[..n])));}}}
    if choices.is_empty(){return Err("no_removable_volume".into());}
    let list=choices.iter().map(|(m,l)|format!("{}  {}",m,l)).collect::<Vec<_>>().join("\n");
    let chosen=entry(&format!("Choose a removable volume explicitly. Type its drive letter (for example E).\n{}\n\nThe file is saved at the root of this volume. Remove and store it after activation. FAT/exFAT may not support private permissions.",list),false)?;
    let letter=chosen.trim().to_ascii_uppercase();
    if letter.len()!=1{return Err("removable_volume_required".into());}
    let mount=choices.into_iter().find(|(m,_)|m.starts_with(&letter)).ok_or_else(||"removable_volume_required".to_string())?.0;Volume::pin(mount)
}}
pub fn publish(from:&Path,to:&Path)->Result<(),String>{
    // Deliberately omit MOVEFILE_REPLACE_EXISTING. No race may overwrite an
    // unexpected destination, even if it appeared after the preflight check.
    if unsafe{MoveFileExW(path_wide(from).as_ptr(),path_wide(to).as_ptr(),MOVEFILE_WRITE_THROUGH)}==0{return Err("recovery_write_failed".into());}Ok(())
}
fn private_descriptor()->Result<*mut std::ffi::c_void,String>{unsafe{
    // Resolve the actual process user SID, rather than trusting USERNAME or an
    // elevated token's default owning group. The owner and sole allow ACE are
    // explicitly this user, even when the app was launched elevated.
    let mut token=null_mut();if OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&mut token)==0{return Err("private_acl_failed".into());}
    let _token=OwnedHandle::from_raw_handle(token);let mut length=0;
    GetTokenInformation(token,TokenUser,null_mut(),0,&mut length);
    if (length as usize)<std::mem::size_of::<TOKEN_USER>(){return Err("private_acl_failed".into());}
    let mut buffer=vec![0usize;(length as usize).div_ceil(std::mem::size_of::<usize>())];
    if GetTokenInformation(token,TokenUser,buffer.as_mut_ptr().cast(),length,&mut length)==0{return Err("private_acl_failed".into());}
    let user=&*buffer.as_ptr().cast::<TOKEN_USER>();let mut sid=null_mut();
    if IsValidSid(user.User.Sid)==0||ConvertSidToStringSidW(user.User.Sid,&mut sid)==0{return Err("private_acl_failed".into());}
    let mut count=0;while count<184 && *sid.add(count)!=0{count+=1;}
    if count==184{LocalFree(sid.cast());return Err("private_acl_failed".into());}
    let name=String::from_utf16_lossy(std::slice::from_raw_parts(sid,count));LocalFree(sid.cast());
    let sddl=wide(&format!("O:{}D:P(A;;FA;;;{})",name,name));let mut descriptor=null_mut();
    if ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.as_ptr(),1,&mut descriptor,null_mut())==0{return Err("private_acl_failed".into());}Ok(descriptor)
}}
pub fn secure_create(p:&Path)->Result<std::fs::File,String>{unsafe{
    // Protected current-user DACL at creation, never after plaintext is written.
    // Unsupported ACL filesystems (FAT/exFAT) receive the explicit picker warning.
    let mut volume=[0u16;1024];let mut flags=0;
    if GetVolumePathNameW(path_wide(p).as_ptr(),volume.as_mut_ptr(),1024)==0 || GetVolumeInformationW(volume.as_ptr(),null_mut(),0,null_mut(),null_mut(),&mut flags,null_mut(),0)==0{return Err("volume_unavailable".into());}
    let private=flags&FILE_PERSISTENT_ACLS!=0;
    let descriptor=if private{private_descriptor()?}else{null_mut()};
    let attrs=SECURITY_ATTRIBUTES{nLength:std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,lpSecurityDescriptor:descriptor,bInheritHandle:0};
    let h=CreateFileW(path_wide(p).as_ptr(),GENERIC_WRITE,0,if private{&attrs}else{null()},CREATE_NEW,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_WRITE_THROUGH|FILE_FLAG_OPEN_REPARSE_POINT,null_mut());
    if !descriptor.is_null(){LocalFree(descriptor);}if h==INVALID_HANDLE_VALUE{return Err("recovery_write_failed".into());}Ok(std::fs::File::from_raw_handle(h))
}}
pub fn read_stick(b:&Binding,printed:bool)->Result<Stick,String>{
    if printed{
        // Printed output is the exact stick JSON, entered only in a protected
        // native control. The same strict parser/binding rules apply.
        let mut typed=entry("Enter the complete printed recovery JSON, including identifiers, UTC date and checksum. Clipboard input is disabled.",true)?;
        let result=parse(typed.as_bytes(),b);wipe(&mut typed);return result;
    }
    let volume=pick_volume()?;volume.validate()?;
    let mut filename=[0u16;1024];let initial=wide(&volume.mount);let title=wide("Select recovery JSON on the chosen removable volume");
    let filter=wide("Recovery JSON\0*.json\0\0");
    let mut ofn:OPENFILENAMEW=unsafe{std::mem::zeroed()};ofn.lStructSize=std::mem::size_of::<OPENFILENAMEW>() as u32;ofn.lpstrFile=filename.as_mut_ptr();ofn.nMaxFile=1024;ofn.lpstrInitialDir=initial.as_ptr();ofn.lpstrTitle=title.as_ptr();ofn.lpstrFilter=filter.as_ptr();ofn.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR|OFN_DONTADDTORECENT|OFN_NODEREFERENCELINKS;
    if unsafe{GetOpenFileNameW(&mut ofn)}==0{return Err("native_cancelled".into());}
    let n=filename.iter().position(|x|*x==0).ok_or_else(fixed)?;let selected=std::path::PathBuf::from(String::from_utf16_lossy(&filename[..n]));
    // Deliberately root-only: there are no traversed directories, junctions,
    // cloud providers or relative paths. A file cannot redirect the connection.
    if selected.parent()!=Some(Path::new(&volume.mount)){return Err("stick_volume_escape".into());}
    let name=selected.file_name().ok_or_else(fail)?;let real=Path::new(&volume.guid).join(name);volume.validate()?;
    let mut bytes=bounded_read(&real)?;volume.validate()?;let result=parse(&bytes,b);for x in &mut bytes{unsafe{std::ptr::write_volatile(x,0)}}result
}
pub fn save(s:&Stick,print:bool)->Result<(),String>{
    if print{return print_stick(s);}
    let v=pick_volume()?;verified_write(Path::new(&v.guid),s,||v.validate())
}
fn print_stick(s:&Stick)->Result<(),String>{
    dialog("Print recovery material?\nThe Windows print system and selected printer receive the recovery code and may retain it. Bob creates no spool copy.\n\nOutput is PENDING until the server receipt proves activation. Cancel leaves old factors authoritative.",false,false)?;
    unsafe{
        let mut pd:PRINTDLGW=std::mem::zeroed();pd.lStructSize=std::mem::size_of::<PRINTDLGW>() as u32;pd.Flags=PD_RETURNDC|PD_NOSELECTION|PD_NOPAGENUMS|PD_DISABLEPRINTTOFILE|PD_HIDEPRINTTOFILE;pd.nCopies=1;
        if PrintDlgW(&mut pd)==0{return Err("native_cancelled".into());}
        let result=(||->Result<(),String>{
            if pd.hDC.is_null()||pd.Flags&PD_PRINTTOFILE!=0{return Err("print_unavailable".into());}
            let name=wide("Bob recovery - PENDING");let mut doc:DOCINFOW=std::mem::zeroed();doc.cbSize=std::mem::size_of::<DOCINFOW>() as i32;doc.lpszDocName=name.as_ptr();
            if StartDocW(pd.hDC,&doc)<=0{return Err("print_failed".into());}
            let mut text=serde_json::to_string_pretty(s).map_err(|_|fail())?;
            let page=(||->Result<(),String>{
                if StartPage(pd.hDC)<=0{return Err("print_failed".into());}
                // Scale to printer resolution; wrap long identifiers without
                // changing JSON (break only between JSON fields, never codes).
                let dpi=GetDeviceCaps(pd.hDC,LOGPIXELSY as i32).max(72);let line=dpi/5;let x=dpi/2;
                let columns=text.lines().map(str::len).max().unwrap_or(60).max(60) as i32;
                let width=(GetDeviceCaps(pd.hDC,HORZRES as i32)-dpi).max(1)/columns;
                let font=CreateFontW((dpi/9).min(width*2),width,0,0,400,0,0,0,0,0,0,0,0,wide("Consolas").as_ptr());if font.is_null(){return Err("print_failed".into());}
                let old=SelectObject(pd.hDC,font);let mut y=dpi/2;let mut ok=true;
                for row in std::iter::once("Bob recovery - PENDING until server activation receipt").chain(text.lines()){
                    let mut w=wide(row);if TextOutW(pd.hDC,x,y,w.as_ptr(),(w.len()-1) as i32)==0{ok=false;}for b in &mut w{std::ptr::write_volatile(b,0);}y+=line;
                }
                SelectObject(pd.hDC,old);DeleteObject(font);
                if !ok||EndPage(pd.hDC)<=0||EndDoc(pd.hDC)<=0{return Err("print_failed".into());}Ok(())
            })();wipe(&mut text);if page.is_err(){AbortDoc(pd.hDC);}page
        })();
        if !pd.hDC.is_null(){DeleteDC(pd.hDC);}if !pd.hDevMode.is_null(){GlobalFree(pd.hDevMode);}if !pd.hDevNames.is_null(){GlobalFree(pd.hDevNames);}
        result?;
    }
    // Spool admission alone does not mean the owner accepted physical output.
    dialog("Only continue after receiving and accepting a complete, legible printout. Cancel means material is NOT saved. Keep it PENDING until the activation receipt.",false,false).map(|_|())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protected_user_acl_is_materialized_on_ntfs() {
        let dir=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        fs::create_dir(&dir).unwrap();let path=dir.join("private-test.json");
        let mut file=secure_create(&path).unwrap();file.write_all(b"synthetic fixture").unwrap();file.sync_all().unwrap();drop(file);
        unsafe {
            let mut owner=null_mut();let mut dacl=null_mut();let mut descriptor=null_mut();
            assert_eq!(GetNamedSecurityInfoW(path_wide(&path).as_ptr(),SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,&mut owner,null_mut(),&mut dacl,null_mut(),&mut descriptor),0);
            assert!(!owner.is_null()&&!dacl.is_null());
            let mut control=0;let mut revision=0;
            assert_ne!(GetSecurityDescriptorControl(descriptor,&mut control,&mut revision),0);
            assert_ne!(control&SE_DACL_PROTECTED,0);assert_eq!((*dacl).AceCount,1);
            let mut ace=null_mut();assert_ne!(GetAce(dacl,0,&mut ace),0);
            let allow=&*ace.cast::<ACCESS_ALLOWED_ACE>();assert_eq!(allow.Header.AceFlags&INHERITED_ACE as u8,0);
            let sid=ace.cast::<u8>().add(std::mem::offset_of!(ACCESS_ALLOWED_ACE,SidStart)).cast();
            assert_ne!(EqualSid(owner,sid),0);
            let expected=private_descriptor().unwrap();let mut expected_owner=null_mut();let mut defaulted=0;
            assert_ne!(GetSecurityDescriptorOwner(expected,&mut expected_owner,&mut defaulted),0);assert_ne!(EqualSid(owner,expected_owner),0);
            LocalFree(expected);LocalFree(descriptor);
        }
        fs::remove_dir_all(dir).unwrap();
    }
}
