"""Isolated desktop recovery tests; no provider or real account access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector import Queue, prepare, prepared_file
from desktop_recovery import resolve_job, guard_pending_uploads, prepare_cached_revision


class DesktopRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queues = []
        self.addCleanup(lambda: [q.db.close() for q in self.queues])
        for name in ('socket.socket.connect', 'socket.socket.connect_ex', 'collector.run_managed'):
            guard = patch(name, side_effect=AssertionError('No network or downloader'))
            guard.start(); self.addCleanup(guard.stop)
        self.source = 'https://weixin.qq.com/sph/offline-recovery'
        self.categories = {'二创': 'folder-a', '其他': 'folder-b'}
        self.media = b'\0\0\0\x18ftypmp42' + b'x' * 128

    def queue(self, digit):
        q = Queue(self.root / (digit * 64))
        self.queues.append(q)
        return q

    def task(self, queue, note='旧点评 #二创', source=None):
        ident = queue.submit_note(source or self.source, note, self.categories)
        return queue.get(ident)

    def plan(self, task):
        return {'url': task['source'], 'comment': task['comment'],
                'parent_fid': task['parent_fid'], 'filename': task['comment'] + '.mp4'}

    def conflict(self):
        q = self.queue('a'); task = self.task(q)
        source = self.root / 'fixture.mp4'; source.write_bytes(self.media)
        local_task = dict(task, source=str(source))
        # Build a valid receipt bound to the real synthetic share identity.
        local = prepare(local_task, q.root, {})
        receipt = local.parent / 'prepared.json'
        data = json.loads(receipt.read_text(encoding='utf-8'))
        data['task']['source'] = task['source']
        receipt.write_text(json.dumps(data), encoding='utf-8')
        q.update(task['id'], 'name_conflict')
        return q, q.get(task['id']), local

    def revision(self):
        old_q, old, source_file = self.conflict()
        q = self.queue('b'); task = self.task(q, '新点评 #其他')
        ref = {'job_key': old_q.root.name, 'task_id': old['id']}
        return old_q, old, source_file, q, task, ref

    def test_equivalent_parsed_note_resolves_old_raw_key(self):
        q = self.queue('a'); task = self.task(q, '旧点评    #二创')
        self.assertEqual(resolve_job(self.root, self.plan(task), 'b' * 64), q.root)

    def test_existing_key_is_kept_even_when_another_database_is_corrupt(self):
        q = self.queue('a'); task = self.task(q)
        bad = self.root / ('b' * 64); bad.mkdir(); (bad / 'tasks.sqlite').write_text('broken')
        self.assertEqual(resolve_job(self.root, self.plan(task), q.root.name), q.root)

    def test_multiple_matches_and_corrupt_history_stop_new_job(self):
        first = self.queue('a'); task = self.task(first)
        second = self.queue('b'); self.task(second)
        with self.assertRaises(ValueError): resolve_job(self.root, self.plan(task), 'c' * 64)
        second.db.close(); self.queues.remove(second)
        (second.root / 'tasks.sqlite').write_text('broken')
        with self.assertRaises(ValueError): resolve_job(self.root, self.plan(task), 'c' * 64)

    def test_pending_upload_reserves_same_source_and_same_destination_across_jobs(self):
        q = self.queue('a'); original = self.task(q); q.update(original['id'], 'needs_review')
        changed_source = dict(self.plan(original), url=self.source + '-other')
        with self.assertRaisesRegex(ValueError, original['id']):
            guard_pending_uploads(self.root, self.root / ('b' * 64), changed_source)
        changed_destination = dict(self.plan(original), comment='其他点评', parent_fid='folder-b')
        with self.assertRaises(ValueError):
            guard_pending_uploads(self.root, self.root / ('b' * 64), changed_destination)
        unrelated = dict(changed_destination, url=self.source + '-other')
        guard_pending_uploads(self.root, self.root / ('b' * 64), unrelated)
        guard_pending_uploads(self.root, q.root, self.plan(original))

    def test_conflict_revision_reuses_verified_bytes_and_is_idempotent(self):
        old_q, old, source_file, q, task, ref = self.revision()
        path = prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        self.assertEqual(path.read_bytes(), self.media)
        self.assertEqual(source_file.read_bytes(), self.media)
        self.assertEqual(old_q.get(old['id'])['state'], 'superseded')
        self.assertEqual(prepared_file(task, path.parent), path)
        self.assertEqual(prepare_cached_revision(self.root, q, task, self.plan(task), ref), path)
        marker = json.loads((path.parent / 'desktop-reuse.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['reference'], ref)

    def test_destination_only_revision_reuses_bytes(self):
        old_q, old, source_file = self.conflict()
        q = self.queue('b'); task = self.task(q, '旧点评 #其他')
        path = prepare_cached_revision(self.root, q, task, self.plan(task),
                                       {'job_key': old_q.root.name, 'task_id': old['id']})
        self.assertEqual(path.read_bytes(), source_file.read_bytes())

    def test_missing_source_or_corrupt_source_does_not_download(self):
        old_q, old, source_file, q, task, ref = self.revision()
        source_file.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '缺失或损坏'):
            prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        self.assertEqual(old_q.get(old['id'])['state'], 'name_conflict')
        self.assertFalse((q.root / 'files' / task['id'] / 'prepared.json').exists())
        marker = json.loads((q.root / 'files' / task['id'] / 'desktop-reuse.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['reference'], ref)
        self.assertEqual(marker['verification'], 'pending')

    def test_other_source_and_unknown_upload_are_rejected(self):
        old_q, old, source_file, q, task, ref = self.revision()
        q.db.execute('UPDATE tasks SET source=? WHERE id=?', (self.source + '-other', task['id'])); q.db.commit()
        changed = q.get(task['id'])
        with self.assertRaises(ValueError): prepare_cached_revision(self.root, q, changed, self.plan(changed), ref)
        q.db.execute('UPDATE tasks SET source=? WHERE id=?', (self.source, task['id'])); q.db.commit()
        old_q.update(old['id'], 'needs_review')
        with self.assertRaises(ValueError): prepare_cached_revision(self.root, q, task, self.plan(task), ref)

    def test_interrupted_copy_recovers_from_same_reference_without_download(self):
        old_q, old, source_file, q, task, ref = self.revision()
        with patch('desktop_recovery.shutil.copyfile', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError): prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        marker = q.root / 'files' / task['id'] / 'desktop-reuse.json'
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['reference'], ref)
        self.assertEqual(prepare_cached_revision(self.root, q, task, self.plan(task), ref).read_bytes(), self.media)

    def test_corrupt_target_cannot_be_repaired_by_fresh_download(self):
        old_q, old, source_file, q, task, ref = self.revision()
        path = prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        self.assertEqual(path.read_bytes(), b'corrupt')

    def test_unknown_or_missing_marker_verification_never_authorizes_reuse(self):
        old_q, old, source_file, q, task, ref = self.revision()
        path = prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        marker_path = path.parent / 'desktop-reuse.json'
        original = json.loads(marker_path.read_text(encoding='utf-8'))
        for value in ('unknown', '', None):
            marker = dict(original)
            if value is None:
                marker.pop('verification')
            else:
                marker['verification'] = value
            marker_path.write_text(json.dumps(marker), encoding='utf-8')
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '校验状态无效'):
                prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        self.assertEqual(path.read_bytes(), self.media)

    def test_one_source_cannot_fork_to_multiple_targets(self):
        old_q, old, source_file, q, task, ref = self.revision()
        prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        other = self.queue('c'); another = self.task(other, '另一点评 #二创')
        with self.assertRaises(ValueError):
            prepare_cached_revision(self.root, other, another, self.plan(another), ref)

    def test_crash_before_source_reservation_does_not_allow_another_branch(self):
        from desktop_recovery import _write_json
        old_q, old, source_file, q, task, ref = self.revision()
        def interrupted(path, data):
            if path.name == 'desktop-revision.json':
                raise OSError('crash before source reservation')
            return _write_json(path, data)
        with patch('desktop_recovery._write_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        other = self.queue('c'); another = self.task(other, '另一点评 #其他')
        with self.assertRaises(ValueError):
            prepare_cached_revision(self.root, other, another, self.plan(another), ref)
        self.assertEqual(prepare_cached_revision(self.root, q, task, self.plan(task), ref).read_bytes(), self.media)

    def test_missing_target_after_copy_can_resume_only_from_its_verified_source(self):
        old_q, old, source_file, q, task, ref = self.revision()
        path = prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        path.unlink(); (path.parent / 'prepared.json').unlink()
        self.assertEqual(prepare_cached_revision(self.root, q, task, self.plan(task), ref).read_bytes(), self.media)

    def test_symlinked_job_or_marker_is_rejected(self):
        old_q, old, source_file, q, task, ref = self.revision()
        from desktop_recovery import _linked
        marker = q.root / 'files' / task['id'] / 'desktop-reuse.json'
        with patch('desktop_recovery._linked', side_effect=lambda path: path == old_q.root or _linked(path)):
            with self.assertRaises(ValueError): resolve_job(self.root, self.plan(task), old_q.root.name)
        with patch('desktop_recovery._linked', side_effect=lambda path: path == marker or _linked(path)):
            with self.assertRaises(ValueError): prepare_cached_revision(self.root, q, task, self.plan(task), ref)

    def test_completed_target_and_traversal_are_rejected_without_changes(self):
        old_q, old, source_file, q, task, ref = self.revision()
        q.update(task['id'], 'succeeded')
        with self.assertRaises(ValueError): prepare_cached_revision(self.root, q, task, self.plan(task), ref)
        with self.assertRaises(ValueError): resolve_job(self.root, self.plan(task), '../escape')
        with self.assertRaises(ValueError):
            guard_pending_uploads(self.root, self.root.parent / ('c' * 64), self.plan(task))
        self.assertEqual(source_file.read_bytes(), self.media)


if __name__ == '__main__':
    unittest.main()
