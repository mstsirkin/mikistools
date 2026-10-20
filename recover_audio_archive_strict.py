#!/usr/bin/env python3
"""Salvage CRC-valid, fully decodable audio from ZIPs in a mandatory offline sandbox.
Never repairs bytes, executes members, follows archive paths, or updates the catalog.
"""
import argparse, hashlib, json, os, pathlib, resource, shutil, stat, subprocess, sys, tempfile, time, zipfile
MAX_ARCHIVE=2*1024**3
MAX_MEMBER=256*1024**2
MAX_TOTAL=2*1024**3
MAX_ENTRIES=2000
FORMATS={'.mp3':'mp3','.wav':'wav','.flac':'flac','.m4a':'mov','.ogg':'ogg','.opus':'ogg','.aac':'aac','.ape':'ape','.wma':'asf','.aif':'aiff','.aiff':'aiff'}

def digest(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()

def unsafe(info):
 name=info.filename.replace('\\','/')
 parts=name.split('/')
 mode=info.external_attr>>16
 if len(name)>1024 or any(ord(c)<32 for c in name):return 'unsafe_name'
 if name.startswith('/') or any(p in ('.','..') or ':' in p for p in parts):return 'unsafe_path'
 if stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR):return 'non_regular_entry'
 if info.flag_bits&1:return 'encrypted_entry'
 if info.file_size>MAX_MEMBER:return 'member_size_limit'
 if info.file_size>max(info.compress_size,1)*200:return 'compression_ratio_limit'
 return None

def worker():
 out=pathlib.Path('/output');rows=[];expanded=0
 report={'format':'zip','state':'partial_recovery','members':rows,'archive_complete':False}
 try:
  with zipfile.ZipFile('/input.archive') as z:
   infos=z.infolist()
   if len(infos)>MAX_ENTRIES:raise ValueError('entry_count_limit')
   for ordinal,info in enumerate(infos):
    row={'ordinal':ordinal,'member':info.filename,'declared_bytes':info.file_size};rows.append(row)
    reason=unsafe(info)
    if reason:row.update(status='rejected',reason=reason);continue
    if info.is_dir():row.update(status='skipped',reason='directory');continue
    suffix=pathlib.Path(info.filename).suffix.lower()
    if suffix not in FORMATS:row.update(status='skipped',reason='not_audio');continue
    if expanded+info.file_size>MAX_TOTAL:row.update(status='rejected',reason='total_expansion_limit');continue
    # No member-controlled output path; never extractall or restore permissions.
    name=f'{ordinal:06d}'+suffix;part=out/(name+'.part');dest=out/name
    count=0
    try:
     with z.open(info) as src,part.open('xb') as dst:
      while b:=src.read(1024*1024):
       count+=len(b);expanded+=len(b)
       if count>MAX_MEMBER or expanded>MAX_TOTAL:raise ValueError('expansion_limit')
       dst.write(b)
     if count!=info.file_size:raise ValueError('member_size_mismatch')
     if count==0:raise ValueError('empty_audio')
     # Full decode, fixed format, no external protocols; entire process is sandboxed.
     cmd=['/usr/bin/ffmpeg','-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','file,pipe','-f',FORMATS[suffix],'-i',str(part),'-map','0:a:0','-vn','-sn','-dn','-filter_threads','1','-f','null','-']
     with tempfile.TemporaryFile() as log:
      result=subprocess.run(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,timeout=60,env={'PATH':'/usr/bin','OMP_NUM_THREADS':'1'})
      if result.returncode:raise ValueError('audio_decode_failed')
     sha=digest(part);part.rename(dest);dest.chmod(0o600)
     row.update(status='recovered',file=name,bytes=count,sha256=sha,zip_crc_valid=True,full_audio_decode=True)
    except Exception as e:
     row.update(status='rejected',reason=type(e).__name__+': '+str(e)[:160])
    finally:part.unlink(missing_ok=True)
 except Exception as e:report.update(state='unreadable_archive',error=type(e).__name__+': '+str(e)[:160])
 report['recovered']=sum(r.get('status')=='recovered' for r in rows)
 report['rejected']=sum(r.get('status')=='rejected' for r in rows)
 report['expanded_bytes_read']=expanded
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=True,indent=2)+'\n')

def limits():
 resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
 resource.setrlimit(resource.RLIMIT_CPU,(300,300))
 resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_MEMBER,MAX_MEMBER))
 resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
 resource.setrlimit(resource.RLIMIT_CORE,(0,0))

def recover(source,destination):
 source=pathlib.Path(source)
 if source.is_symlink() or not source.is_file():raise ValueError('Input must be a regular file, not a symlink')
 source=source.resolve();destination=pathlib.Path(destination).resolve()
 if source.stat().st_size>MAX_ARCHIVE:raise ValueError('Archive exceeds 2GiB recovery limit')
 if not shutil.which('bwrap'):raise RuntimeError('Required bubblewrap sandbox unavailable; refusing unsandboxed recovery')
 destination.mkdir(parents=True,exist_ok=True,mode=0o700)
 if shutil.disk_usage(destination).free<MAX_TOTAL+5*1024**3:raise RuntimeError('Insufficient disk reserve')
 before=(source.stat().st_size,source.stat().st_mtime_ns);sha=digest(source);final=destination/sha
 if final.exists():raise FileExistsError('Recovery already exists; inspect existing report instead of overwriting')
 with tempfile.TemporaryDirectory(prefix='.recovery-',dir=destination) as staging:
  # Snapshot prevents a concurrent writer changing the archive while it is parsed.
  snapshot=pathlib.Path(staging)/'input';shutil.copyfile(source,snapshot)
  if digest(snapshot)!=sha or before!=(source.stat().st_size,source.stat().st_mtime_ns):raise RuntimeError('Source changed during snapshot')
  output=pathlib.Path(staging)/'output';output.mkdir(mode=0o700)
  cmd=['bwrap','--unshare-all','--die-with-parent','--new-session','--ro-bind','/usr','/usr','--ro-bind','/etc/ld.so.cache','/etc/ld.so.cache','--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64','--symlink','usr/bin','/bin','--proc','/proc','--dev','/dev','--tmpfs','/tmp','--cap-drop','ALL','--ro-bind',str(snapshot),'/input.archive','--ro-bind',str(pathlib.Path(__file__).resolve()),'/recover.py','--bind',str(output),'/output','--chdir','/output','/usr/bin/python3','-I','/recover.py','--worker']
  with tempfile.TemporaryFile() as log:
   r=subprocess.run(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=log,timeout=360,preexec_fn=limits,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
   if r.returncode:raise RuntimeError('Sandboxed recovery failed; no files promoted')
  receipt=output/'report.json'
  if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size>8*1024**2:raise ValueError('Unsafe or oversized recovery report')
  report=json.loads(receipt.read_text())
  # Recheck generated objects outside the parser; only regular, declared files survive.
  expected={'report.json'}
  for row in report['members']:
   if row.get('status')!='recovered':continue
   name=row['file']
   if pathlib.Path(name).name!=name:raise ValueError('Invalid recovery output name')
   f=output/name
   if f.is_symlink() or not f.is_file() or f.stat().st_nlink!=1:raise ValueError('Unsafe recovery output')
   if f.stat().st_size!=row['bytes'] or digest(f)!=row['sha256']:raise ValueError('Recovery output integrity mismatch')
   expected.add(name)
  if {p.name for p in output.iterdir()}!=expected:raise ValueError('Unexpected sandbox outputs')
  report.update(source_archive=str(source),source_sha256=sha,completed_at=time.time(),security={'network':False,'home_access':False,'source_read_only':True,'sandbox':'bubblewrap','max_member_bytes':MAX_MEMBER,'max_total_bytes':MAX_TOTAL},catalog_modified=False)
  (output/'report.json').write_text(json.dumps(report,ensure_ascii=True,indent=2)+'\n')
  output.rename(final)
 return report

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('archive',nargs='?');p.add_argument('--output');p.add_argument('--worker',action='store_true');a=p.parse_args()
 if a.worker:worker();return
 if not a.archive or not a.output:p.error('archive and --output are required')
 r=recover(a.archive,a.output);print(json.dumps({k:r.get(k) for k in ('state','recovered','rejected','source_sha256','error')}))
if __name__=='__main__':main()
