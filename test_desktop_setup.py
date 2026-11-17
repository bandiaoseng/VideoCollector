import unittest,tempfile,json,os
from pathlib import Path
from unittest.mock import patch
from desktop_setup import save_settings

class FirstRunSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'config.json'
        self.path.write_text(Path('config.example.json').read_text(encoding='utf-8'),encoding='utf-8')
    def test_local_save_preserves_config_and_rejects_duplicates(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('Unexpected network')):
            result=save_settings(self.path,'二创=视频素材/二创')
        self.assertEqual(result['quark_web']['category_paths']['二创'],['视频素材','二创'])
        self.assertFalse(result['zhiling']['enabled'])
        self.assertFalse(Path(result['quark_web']['session_file']).exists())
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):save_settings(self.path,'二创=甲\n二创=乙')
        self.assertEqual(before,self.path.read_bytes())
    def test_blank_keys_preserve_existing_settings(self):
        sample='unit-test-placeholder'
        with patch.dict(os.environ,{},clear=False):
            result=save_settings(self.path,'演示=测试',zhiling_key=sample,zhiling_enabled=True,dtk_url='http://127.0.0.1:9000',dtk_key=sample)
            again=save_settings(self.path,'演示=测试',zhiling_enabled=True,dtk_url='http://127.0.0.1:9000')
        self.assertEqual(Path(again['zhiling']['api_key_file']).read_text(),sample)
        self.assertNotIn(sample,self.path.read_text())
    def test_missing_keys_and_unsafe_service_url_are_rejected(self):
        for kwargs in ({'zhiling_enabled':True},{'dtk_url':'http://public.example.invalid'},{'dtk_url':'https://name:password@example.invalid'}):
            with self.assertRaises(ValueError):save_settings(self.path,'演示=测试',**kwargs)

if __name__=='__main__':unittest.main()
