"""Durable incoming shares. A draft owns exactly one idempotent upload request."""
import time
import uuid
from pathlib import PureWindowsPath

from collector import Queue, video_name
from share_input import parse_share
from parse_recovery import parser_recovery


class DraftConflict(ValueError):
    def __init__(self, current):
        super().__init__('点评已在其他页面更新，请先比较两份内容，再选择保留哪一份')
        self.current = current


class DraftStore:
    def __init__(self, root):
        self.queue = Queue(root)
        self.db = self.queue.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, share_text TEXT NOT NULL,
            platform TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created REAL NOT NULL)''')
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if 'version' not in {row[1] for row in self.db.execute('PRAGMA table_info(drafts)')}:
                self.db.execute('ALTER TABLE drafts ADD COLUMN version INTEGER NOT NULL DEFAULT 0')
            if 'cancelled' not in {row[1] for row in self.db.execute('PRAGMA table_info(drafts)')}:
                self.db.execute('ALTER TABLE drafts ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0')
            if 'revision_of' not in {row[1] for row in self.db.execute('PRAGMA table_info(drafts)')}:
                self.db.execute('ALTER TABLE drafts ADD COLUMN revision_of TEXT')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_revision_per_draft ON drafts(revision_of) WHERE revision_of IS NOT NULL')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def create(self, draft_id, share_text):
        share = parse_share(share_text)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT source FROM drafts WHERE id=?', (draft_id,)).fetchone()
            if old and old['source'] != share['url']:
                raise ValueError('这个提交编号已用于另一条视频，请重新提交')
            if not old:
                self.db.execute('INSERT INTO drafts (id,source,share_text,platform,created) VALUES (?,?,?,?,?)',
                                (draft_id, share['url'], share_text, share['platform'], time.time()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.get(draft_id)

    def get(self, draft_id):
        row = self.db.execute('SELECT * FROM drafts WHERE id=?', (draft_id,)).fetchone()
        if row is None:
            raise KeyError('收藏不存在')
        draft = dict(row)
        child = self.db.execute('SELECT id FROM drafts WHERE revision_of=?', (draft_id,)).fetchone()
        draft['revision_id'] = child['id'] if child else None
        task = self.db.execute('SELECT * FROM tasks WHERE request_key=?', ('draft:' + draft_id,)).fetchone()
        # Never expose server paths, cookies, opaque folder IDs, or downloader diagnostics.
        draft.update(state='cancelled' if draft['cancelled'] else 'awaiting_note', task_id=None, filename=None, category=None, notice='', review_note='')
        if task:
            # Preserve the actual prepared/uploaded name across rule upgrades.
            # PureWindowsPath strips both Windows and POSIX directories; never
            # expose an old machine's absolute path after a data migration.
            filename = (PureWindowsPath(task['local_file']).name if task['local_file'] else None)
            draft.update(state=task['state'], task_id=task['id'], note=task['input_text'],
                         filename=filename or video_name(task['comment'], '.mp4'), category=task['category'],
                         notice=task['notice'] or '', review_note=task['review_note'] or '')
            if task['state'] == 'prepare_failed' and task['prepared_from'] is None:
                draft.update(parser_recovery(self.queue.root / 'files' / task['id'], task['source']))
        return draft

    def all(self, limit=100, offset=0):
        rows = self.db.execute('SELECT id FROM drafts ORDER BY created DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return [self.get(row['id']) for row in rows]

    def save_note(self, draft_id, note, version):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            draft = self.get(draft_id)
            if draft['task_id'] or draft['cancelled']:
                raise ValueError('已经提交或取消收藏，不能修改点评')
            if note != draft['note']:
                if version != draft['version']:
                    raise DraftConflict(draft)
                self.db.execute('UPDATE drafts SET note=?,version=version+1 WHERE id=?', (note, draft_id))
            result = self.get(draft_id)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return result

    def submit(self, draft_id, note, categories, version):
        # The link to tasks is derived from request_key, so a crash after queue commit
        # cannot leave an uploaded task detached from its draft. Serialize double clicks.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            draft = self.get(draft_id)
            if draft['cancelled']:
                raise ValueError('这条收藏已取消，不能继续提交')
            if not draft['task_id'] and (version != draft['version'] or note != draft['note']):
                raise DraftConflict(draft)
            original = self.get(draft['revision_of'])['task_id'] if draft['revision_of'] else None
            self.queue.submit_note(draft['source'], note, categories, 'draft:' + draft_id, prepared_from=original)
        except BaseException:
            self.db.rollback()
            raise
        return self.get(draft_id)

    def cancel(self, draft_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            draft = self.get(draft_id)
            if draft['task_id']:
                self.queue.cancel(draft['task_id'])
            elif not draft['cancelled']:
                self.db.execute('UPDATE drafts SET cancelled=1,version=version+1 WHERE id=?', (draft_id,))
            result = self.get(draft_id)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def revise(self, draft_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            draft = self.get(draft_id)
            if draft['revision_id']:
                revised = self.get(draft['revision_id'])
            else:
                if draft['state'] != 'name_conflict' or not draft['task_id']:
                    raise ValueError('只有尚未上传的同名冲突记录可以修改；请先查看当前状态')
                new_id = str(uuid.uuid4())
                self.db.execute('''INSERT INTO drafts
                    (id,source,share_text,platform,note,created,revision_of) VALUES (?,?,?,?,?,?,?)''',
                    (new_id,draft['source'],draft['share_text'],draft['platform'],draft['note'],time.time(),draft_id))
                self.db.execute("UPDATE tasks SET state='superseded',updated=? WHERE id=? AND state='name_conflict'", (time.time(),draft['task_id']))
                revised = self.get(new_id)
            result = {'original':self.get(draft_id), 'draft':revised}
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise
