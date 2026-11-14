import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from offline_preview import categories_from_file, preview, build_window


class OfflinePreviewTests(unittest.TestCase):
    def test_preview_uses_real_rules_without_any_io_side_effects(self):
        with patch('socket.socket.connect', side_effect=AssertionError('No network')), \
             patch('subprocess.Popen', side_effect=AssertionError('No child process')):
            result = preview('分享 https://weixin.qq.com/sph/offline-test', '值得再看 #二创', {'二创': 'fixture'})
        self.assertEqual(result['filename'], '值得再看.mp4')
        self.assertEqual(result['category'], '二创')
        self.assertEqual(result['state'], 'input_checked')
        self.assertFalse(result['downloaded'])
        self.assertFalse(result['uploaded'])

    def test_invalid_input_never_looks_successful(self):
        for source, note in [('no-link', '点评 #二创'),
                             ('https://weixin.qq.com/sph/test', '点评'),
                             ('https://weixin.qq.com/sph/test', '点评 #不存在'),
                             ('https://weixin.qq.com/sph/one https://weixin.qq.com/sph/two', '点评 #二创')]:
            with self.subTest(source=source, note=note), self.assertRaises(ValueError):
                preview(source, note, {'二创': 'fixture'})

    def test_filename_change_is_disclosed(self):
        result = preview('https://v.douyin.com/offline/', 'CON #二创', {'二创': 'fixture'})
        self.assertEqual(result['filename'], '_CON.mp4')
        self.assertTrue(result['filename_adjusted'])

    def test_reading_categories_does_not_modify_live_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            content = json.dumps({'categories': {'二创': 'fixture'}, 'zhiling': {'enabled': False}})
            path.write_text(content, encoding='utf-8')
            self.assertEqual(categories_from_file(path), {'二创': 'fixture'})
            self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_real_widgets_do_not_retain_stale_success(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'categories': {'二创': 'fixture'}}), encoding='utf-8')
            try:
                window = build_window(path)
            except tk.TclError as exc:
                self.skipTest(f'Tk display unavailable: {exc}')
            self.addCleanup(window.destroy)
            window.withdraw()
            source, note, button, result = window.preview_controls
            source.insert('1.0', 'https://weixin.qq.com/sph/offline-test')
            note.insert('1.0', '点评 #二创')
            window.update()
            button.invoke()
            self.assertIn('不是归档成功', result.get())
            note.insert('end', ' #另一个类目')
            window.update()
            self.assertIn('内容已修改', result.get())
            button.invoke()
            self.assertIn('输入还需修改', result.get())


if __name__ == '__main__':
    unittest.main()
