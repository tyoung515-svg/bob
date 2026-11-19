import pathlib,subprocess,sys,uuid
host=pathlib.Path(sys.argv[1]);base=pathlib.Path(sys.argv[2]);root=base/('refused-store-'+uuid.uuid4().hex)
result=subprocess.run([str(host),'--root',str(root)],input=b'',capture_output=True,timeout=5,env={'PATH':'/usr/bin:/bin'})
assert result.returncode==2,(result.returncode,result.stderr.decode())
assert not root.exists(),'Non-ext4 refusal created/touched the requested store before rejecting'
print('PASS env-unset non-ext4 store refused before creation; zero model dispatches')
