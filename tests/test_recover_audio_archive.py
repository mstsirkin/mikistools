import importlib.util, pathlib, tempfile, unittest, zipfile, io, wave, math, array, stat, struct
TOOL=pathlib.Path(__file__).resolve().parents[1]/'recover_audio_archive.py'
spec=importlib.util.spec_from_file_location('recover_audio_archive',TOOL);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

def sound():
 b=io.BytesIO()
 with wave.open(b,'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000)
  w.writeframes(array.array('h',(int(10000*math.sin(i*.2)) for i in range(8000))).tobytes())
 return b.getvalue()

class RecoveryTests(unittest.TestCase):
 def test_security_and_crc_salvage(self):
  with tempfile.TemporaryDirectory() as temp:
   d=pathlib.Path(temp);zpath=d/'bad.zip'
   with zipfile.ZipFile(zpath,'w',compression=zipfile.ZIP_STORED) as z:
    z.writestr('good.wav',sound());z.writestr('bad.wav',sound())
    z.writestr('../escape.wav',sound());z.writestr('/absolute.wav',sound());z.writestr('C:\\escape.wav',sound())
    link=zipfile.ZipInfo('link.wav');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(link,'/etc/passwd')
    z.writestr('run.sh','exit 1');z.writestr('fake.mp3','#EXTM3U\nhttps://example.invalid/audio.mp3\n')
   with zipfile.ZipFile(zpath) as z:
    i=z.getinfo('bad.wav');offset=i.header_offset+30+len(i.filename.encode())+len(i.extra)+100
   data=bytearray(zpath.read_bytes());data[offset]^=1;zpath.write_bytes(data);before=mod.digest(zpath)
   result=mod.recover(zpath,d/'output')
   self.assertEqual(result['recovered'],1);self.assertEqual(result['rejected'],6)
   self.assertEqual(before,mod.digest(zpath));self.assertFalse((d/'escape.wav').exists())
   files=list((d/'output'/before).iterdir());self.assertEqual({f.name for f in files},{'000000.wav','report.json'})
   self.assertFalse(result['archive_complete'])
   with self.assertRaises(FileExistsError):mod.recover(zpath,d/'output')
 def test_uniform_directory_offset_recovery(self):
  with tempfile.TemporaryDirectory() as temp:
   d=pathlib.Path(temp);p=d/'offset.zip'
   with zipfile.ZipFile(p,'w') as z:
    z.writestr('one.wav',sound());z.writestr('two.wav',sound())
   data=bytearray(p.read_bytes());end=data.rfind(b'PK\x05\x06');offset=struct.unpack_from('<I',data,end+16)[0]
   struct.pack_into('<I',data,end+16,offset-37);p.write_bytes(data)
   original=mod.digest(p);r=mod.recover(p,d/'out')
   self.assertEqual(r['recovered'],2);self.assertEqual(r['uniform_offset_correction_bytes'],37)
   self.assertEqual(mod.digest(p),original)
 def test_expansion_limits(self):
  i=zipfile.ZipInfo('bomb.wav');i.file_size=10**8;i.compress_size=10
  self.assertEqual(mod.unsafe(i),'compression_ratio_limit')
  i.file_size=mod.MAX_MEMBER+1
  self.assertEqual(mod.unsafe(i),'member_size_limit')
 def test_encryption_rejected(self):
  i=zipfile.ZipInfo('private.mp3');i.flag_bits=1
  self.assertEqual(mod.unsafe(i),'encrypted_entry')
 def test_unreadable_archive(self):
  with tempfile.TemporaryDirectory() as temp:
   d=pathlib.Path(temp);p=d/'invalid.zip';p.write_bytes(b'PKbad')
   r=mod.recover(p,d/'out');self.assertEqual(r['state'],'unreadable_archive');self.assertEqual(r['recovered'],0)
 def test_input_symlink_rejected(self):
  with tempfile.TemporaryDirectory() as temp:
   d=pathlib.Path(temp);(d/'file').write_text('x');(d/'link').symlink_to(d/'file')
   with self.assertRaises(ValueError):mod.recover(d/'link',d/'out')
if __name__=='__main__':unittest.main()
