"""Persistent video collection pipeline. Python 3.11+, no third party dependencies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
import re
import shutil
import sqlite3
import sys
import time
import uuid
from share_input import parse_share
from note_input import parse_note
from process_runner import run_managed, ProcessCancelled
from storage_guard import check_free, budget, transfer_check
from upload_evidence import confirmed_upload, evidence_directory, snapshot
from destination_check import check_destination, DestinationUnchecked, NameConflict, AgentEnvironmentUnsupported, name_key, fid_key


VIDEO_SUFFIXES = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'}


def video_name(comment: str, suffix: str) -> str:
    """Retain the full comment in the DB; make a Windows-compatible filename."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', comment)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if not value:
        raise ValueError('点评不能为空或只包含文件名禁用字符')
    if suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError('请选择支持的视频文件')
    # Keep both UTF-16 units and UTF-8 bytes bounded, leaving room for suffix.
    # Never split a Unicode character or silently accept an unpaired surrogate.
    try:
        value = value.encode('utf-16-le')[:240].decode('utf-16-le', errors='ignore')
        value = value.encode('utf-8')[:240].decode('utf-8', errors='ignore').rstrip(' .')
    except UnicodeError:
        raise ValueError('点评包含无法保存的字符，请重新输入该字符') from None
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1, 10)), *('LPT'+str(i) for i in range(1, 10))}:
        value = '_' + value
    return value + suffix.lower()


class Queue:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'tasks.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL, comment TEXT NOT NULL, category TEXT NOT NULL,
            parent_fid TEXT NOT NULL, state TEXT NOT NULL, local_file TEXT,
            file_hash TEXT, remote_fid TEXT, error TEXT, notice TEXT,
            created REAL NOT NULL, updated REAL NOT NULL)''')
        self.db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(tasks)')}
        if 'input_text' not in columns:
            self.db.execute('ALTER TABLE tasks ADD COLUMN input_text TEXT')
        for name in ('upload_attempt', 'upload_snapshot', 'review_note', 'prepared_from'):
            if name not in columns:
                self.db.execute(f'ALTER TABLE tasks ADD COLUMN {name} TEXT')
        self.db.execute('''CREATE TABLE IF NOT EXISTS upload_reviews (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, attempt TEXT,
            outcome TEXT NOT NULL, message TEXT NOT NULL, evidence TEXT, checked REAL NOT NULL)''')
        self.db.commit()

    def submit_note(self, source: str, note: str, categories: dict,
                    request_key: str | None = None, *, prepared_from=None) -> str:
        comment, category = parse_note(note, categories)
        return self.submit(source, comment, category, categories, request_key, input_text=note, prepared_from=prepared_from)

    def submit(self, source: str, comment: str, category: str, categories: dict,
               request_key: str | None = None, *, input_text: str | None = None, prepared_from=None) -> str:
        source, comment, category = source.strip(), comment.strip(), category.strip()
        if not source or not comment:
            raise ValueError('视频来源和点评都必须填写')
        video_name(comment, '.mp4')
        fid = categories.get(category)
        if not isinstance(fid, str) or not fid.strip():
            raise ValueError('该类目尚未绑定夸克文件夹，请先配置')
        key = request_key or uuid.uuid4().hex
        old = self.db.execute('SELECT * FROM tasks WHERE request_key=?', (key,)).fetchone()
        if old:
            if (old['source'], old['comment'], old['category'], old['parent_fid'], old['prepared_from']) != (source, comment, category, fid, prepared_from):
                raise ValueError('同一请求编号不能用于不同的视频或点评')
            return old['id']
        task_id = uuid.uuid4().hex
        now = time.time()
        self.db.execute('''INSERT INTO tasks
            (id,request_key,source,comment,category,parent_fid,state,created,updated,input_text,prepared_from)
            VALUES (?,?,?,?,?,?,'queued',?,?,?,?)''', (task_id, key, source, comment, category, fid, now, now, input_text or comment, prepared_from))
        self.db.commit()
        return task_id

    def get(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        return dict(row)

    def all(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM tasks ORDER BY created DESC')]

    def update(self, task_id, state, **values):
        allowed = {'local_file', 'file_hash', 'remote_fid', 'error', 'notice'}
        if set(values) - allowed:
            raise ValueError('Unsupported update')
        values.update(state=state, updated=time.time())
        self.db.execute('UPDATE tasks SET '+','.join(k+'=?' for k in values)+' WHERE id=?', [*values.values(), task_id])
        self.db.commit()

    def claim(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute("SELECT id FROM tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE tasks SET state='preparing',updated=? WHERE id=?", (time.time(), row['id']))
            self.db.commit()
            return self.get(row['id']) if row else None
        except BaseException:
            self.db.rollback()
            raise

    def recover(self):
        # Only run under the exclusive worker lock. Remote side effects cannot be rolled back.
        self.db.execute("UPDATE tasks SET state='queued' WHERE state='preparing'")
        self.db.execute("UPDATE tasks SET state='needs_review',error='上次上传中断，先核对网盘，避免重复上传' WHERE state='uploading'")
        self.db.execute("UPDATE tasks SET state='cancelled',error=NULL,updated=? WHERE state='cancel_requested'", (time.time(),))
        self.db.commit()
        for row in self.db.execute("SELECT id FROM tasks WHERE state='needs_review' AND upload_attempt IS NOT NULL").fetchall():
            self.review_upload(row['id'])

    def start_upload(self, task, local, checksum):
        if local.parent.resolve() != (self.root / 'files' / task['id']).resolve():
            raise ValueError('上传文件不属于当前任务，未启动上传')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.get(task['id'])['state'] != 'preparing':
                raise ValueError('任务状态已变化，未启动上传')
            others = self.db.execute("SELECT * FROM tasks WHERE id<>? AND state IN ('uploading','needs_review','succeeded')", (task['id'],))
            for other in others:
                other_name = PureWindowsPath(other['local_file']).name if other['local_file'] else video_name(other['comment'], '.mp4')
                if fid_key(other['parent_fid']) == fid_key(task['parent_fid']) and name_key(other_name) == name_key(local.name):
                    raise NameConflict()
            attempt = uuid.uuid4().hex
            bound = dict(task, upload_attempt=attempt)
            evidence_directory(self.root, bound).mkdir(parents=True, exist_ok=False)
            metadata = json.dumps(snapshot(task, local, checksum), ensure_ascii=False)
            self.db.execute('''UPDATE tasks SET state='uploading',local_file=?,file_hash=?,
                upload_attempt=?,upload_snapshot=?,review_note=NULL,error=NULL,updated=? WHERE id=?''',
                (str(local), checksum, attempt, metadata, time.time(), task['id']))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.get(task['id'])

    def review_upload(self, task_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            task = self.get(task_id)
            if task['state'] == 'succeeded':
                self.db.commit()
                return task
            if task['state'] != 'needs_review':
                raise ValueError('上传尚未结束或无需核对，请等待当前任务完成')
            evidence = None
            try:
                evidence = confirmed_upload(self.root, task)
                message = '已根据本次完整上传回执确认成功，没有重复上传。'
            except ValueError as exc:
                message = str(exc)
            now = time.time()
            if evidence:
                self.db.execute('''UPDATE tasks SET state='succeeded',remote_fid=?,notice=?,
                    error=NULL,review_note=?,updated=? WHERE id=?''',
                    (evidence['remote_fid'], '\n\n'.join(dict.fromkeys(filter(None, [task['notice'], evidence['notice']]))), message, now, task_id))
            else:
                self.db.execute('UPDATE tasks SET review_note=?,updated=? WHERE id=?', (message, now, task_id))
            self.db.execute('''INSERT INTO upload_reviews (task_id,attempt,outcome,message,evidence,checked)
                VALUES (?,?,?,?,?,?)''', (task_id, task['upload_attempt'], 'confirmed' if evidence else 'unresolved',
                message, json.dumps(evidence, ensure_ascii=False) if evidence else None, now))
            self.db.commit()
            return self.get(task_id)
        except BaseException:
            self.db.rollback()
            raise

    def retry(self, task_id):
        changed = self.db.execute("UPDATE tasks SET state='queued',error=NULL,updated=? WHERE id=? AND state IN ('prepare_failed','setup_required','destination_unchecked')", (time.time(), task_id))
        self.db.commit()
        if changed.rowcount != 1:
            raise ValueError('此状态不能直接重试；上传结果不明时必须先核对网盘')

    def cancel(self, task_id):
        # Share the draft-store transaction when cancellation races with submit.
        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            task = self.get(task_id)
            state = task['state']
            if state == 'superseded':
                raise ValueError('这条记录已有修改版本，请在修改记录里操作')
            if state in {'queued', 'prepare_failed', 'setup_required', 'name_conflict', 'destination_unchecked'}:
                state = 'cancelled'
            elif state == 'preparing':
                state = 'cancel_requested'
            elif state not in {'cancelled', 'cancel_requested'}:
                raise ValueError('已进入上传阶段或已有上传结果，不能保证撤回；请等待结果或核对夸克')
            if state != task['state']:
                self.db.execute('UPDATE tasks SET state=?,error=NULL,updated=? WHERE id=?', (state, time.time(), task_id))
            result = self.get(task_id)
            if own_transaction:
                self.db.commit()
            return result
        except BaseException:
            if own_transaction:
                self.db.rollback()
            raise

    def finish_cancel(self, task_id):
        self.db.execute("UPDATE tasks SET state='cancelled',error=NULL,updated=? WHERE id=? AND state='cancel_requested'", (time.time(), task_id))
        self.db.commit()
        return self.get(task_id)

    def preparation_failed(self, task_id, state, error, notice=''):
        # A late downloader/preflight error must not resurrect a cancelled task.
        self.db.execute("UPDATE tasks SET state=?,error=?,notice=?,updated=? WHERE id=? AND state='preparing'", (state, error, notice, time.time(), task_id))
        self.db.commit()
        return self.finish_cancel(task_id)


class WorkerLock:
    """OS lock releases on process death; no stale lock-file guessing."""
    def __init__(self, root):
        self.path = Path(root) / 'worker.lock'

    def __enter__(self):
        self.file = self.path.open('a+b')
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('已有上传进程在运行') from None
        return self

    def __exit__(self, *_):
        self.file.close()


def prepared_file(task: dict, folder: Path) -> Path | None:
    """Reuse only this task's named, validated snapshot, never an orphan file."""
    try:
        receipt = json.loads((folder / 'prepared.json').read_text(encoding='utf-8'))
        if not isinstance(receipt, dict) or receipt.get('version') != 1:
            return None
        identity = {key: task[key] for key in ('id', 'source', 'comment')}
        if receipt.get('task') != identity:
            return None
        name = receipt.get('filename')
        if not isinstance(name, str) or name != video_name(task['comment'], Path(name).suffix):
            return None
        target = folder / name
        if target.is_symlink() or not target.is_file():
            return None
        if target.stat().st_size != receipt.get('bytes') or receipt['bytes'] <= 0:
            return None
        if sha256(target) != receipt.get('sha256'):
            return None
        return target
    except (OSError, ValueError, TypeError, KeyError):
        return None


def revision_media(task, root):
    """A revision is local-only; invalid reuse must never become a new download."""
    original = task.get('prepared_from')
    if original is None:
        return None
    if not isinstance(original, str) or not re.fullmatch('[a-f0-9]{32}', original):
        raise ValueError('原视频续接记录无效，已停止；不会重新解析或下载。')
    db = sqlite3.connect(root / 'tasks.sqlite')
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT * FROM tasks WHERE id=?', (original,)).fetchone()
    finally:
        db.close()
    if row is None or row['state'] != 'superseded' or row['source'] != task['source']:
        raise ValueError('原视频续接记录不匹配，已停止；不会重新解析或下载。')
    source = prepared_file(dict(row), root / 'files' / original)
    if source is None:
        raise ValueError('原视频缓存缺失或校验不符，已停止；不会重新解析或下载。')
    return source


def prepare(task: dict, root: Path, config: dict, cancel_check=None) -> Path:
    if cancel_check and cancel_check():
        raise ProcessCancelled('已请求取消')
    check_free(root, config)
    folder = root / 'files' / task['id']
    folder.mkdir(parents=True, exist_ok=True)
    cached = prepared_file(task, folder)
    if cached is not None:
        return cached
    revision = revision_media(task, root)
    if config.get('cached_media_only') and revision is None:
        raise ValueError('已有视频缓存缺失或校验不符，已停止；本次禁止重新下载。')
    is_share = bool(re.search(r'https?://', task['source']))
    source = revision or (Path(task['source']) if not is_share else None)
    # Once bound to an original task, even a file disappearing between validation
    # and copy must fail locally rather than fall through to a paid parser.
    if revision is not None or (source is not None and source.is_file()):
        check_free(folder, config, source.stat().st_size)
        target = folder / video_name(task['comment'], source.suffix)
        staging = folder / 'copy.partial'
        with source.open('rb') as src, staging.open('wb') as dst:
            while block := src.read(1024 * 1024):
                if transfer_check(folder, config, cancel_check):
                    raise ProcessCancelled('已请求取消')
                dst.write(block)
        staging.replace(target)
    else:
        # Downloaders differ by platform. No guessed direct URL or fake MP4 fallback.
        share = parse_share(task['source'])
        allowance = budget(config, share['platform'])
        check_free(folder, config, allowance)
        def monitor():
            return transfer_check(folder, config, cancel_check, limit=allowance)
        if share['platform'] == 'wechat' and config.get('zhiling', {}).get('enabled') is True:
            from zhiling_download import check_settings
            settings = {key: value for key, value in config['zhiling'].items() if key in {
                'enabled', 'api_key_env', 'api_key_file', 'ffprobe', 'ffmpeg',
                'request_timeout', 'validation_timeout', 'max_bytes'}}
            check_settings(settings)
            output = folder / 'download.mp4'
            output.unlink(missing_ok=True)
            result = run_managed([sys.executable, str(Path(__file__).with_name('zhiling_download.py')),
                '--source', share['url'], '--output', str(output),
                '--settings-json', json.dumps(settings, ensure_ascii=True)],
                capture_output=True, timeout=config.get('download_timeout', 600), cancel_check=monitor)
            if result.returncode:
                try:
                    message = json.loads(result.stdout)['error']
                    if not isinstance(message, str):
                        raise ValueError('Invalid error')
                except (ValueError, TypeError, KeyError):
                    message = '视频号下载未成功，未上传'
                raise ValueError(message)
        elif share['platform'] == 'douyin' and config.get('dtk', {}).get('base_url'):
            from dtk_download import download
            output = folder / 'download.mp4'
            download(config['dtk'], share['url'], output, cancel_check=monitor)
        else:
            command = config.get('download_commands', {}).get(share['platform']) or config.get('download_command')
            if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                raise ValueError('该平台的下载服务尚未配置，视频链接和点评已保留')
            output = folder / 'download.mp4'
            output.unlink(missing_ok=True)
            args = [x.replace('{source}', share['url']).replace('{output}', str(output)) for x in command]
            run_managed(args, check=True, timeout=config.get('download_timeout', 600), cancel_check=monitor)
        if monitor():
            raise ProcessCancelled('已请求取消')
        target = folder / video_name(task['comment'], '.mp4')
        output.replace(target)
    if cancel_check and cancel_check():
        raise ProcessCancelled('已请求取消')
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError('下载结果为空，尚未上传')
    # Reject common HTML/JSON error documents masquerading as video files.
    with target.open('rb') as stream:
        head = stream.read(64).lstrip().lower()
    if head.startswith((b'<', b'{', b'[')):
        raise ValueError('下载结果不是视频，而是网页或接口错误')
    # Download adapters may retain receipts for download.mp4; this receipt binds
    # the renamed upload snapshot to its task and survives preparation recovery.
    receipt = {'version': 1, 'task': {key: task[key] for key in ('id', 'source', 'comment')},
               'filename': target.name, 'bytes': target.stat().st_size, 'sha256': sha256(target)}
    temporary = folder / 'prepared.json.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
    temporary.replace(folder / 'prepared.json')
    return target


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class SetupRequired(Exception):
    pass


class QuarkUploader:
    def __init__(self, config):
        self.config = config

    def preflight(self):
        script = Path(self.config.get('quark_cli', ''))
        if not script.is_file() or not self.config.get('quark_authorized'):
            raise SetupRequired('需要先完成夸克授权并配置上传工具')
        if not shutil.which(self.config.get('node', 'node')):
            raise SetupRequired('未找到 Node.js')

    def check_destination(self, path, task, cancel_check=None):
        return check_destination(self.config, path, task, cancel_check)

    def upload(self, path, task):
        session = self.config.get('quark_session_id') or str(int(time.time())) + '-' + uuid.uuid4().hex[:6]
        args = [self.config.get('node', 'node'), self.config['quark_cli'], 'upload',
                '--file-path', str(path), '--parent-fid', task['parent_fid'],
                '--session-id', session, '--session-input', task.get('input_text') or task['comment']]
        output = path.parent / 'upload-attempts' / task['upload_attempt'] / 'stdout.ndjson'
        result = run_managed(args, capture_output=True, stdout_path=output,
                             timeout=self.config.get('upload_timeout', 3600))
        parsed = parse_upload(result.stdout, result.returncode)
        if parsed['state'] == 'succeeded':
            # Apply the same exact file checks to ordinary success as recovery.
            proof = confirmed_upload(path.parent.parent.parent, task)
            parsed.update(remote_fid=proof['remote_fid'], notice=proof['notice'])
        return parsed


def parse_upload(stdout: str, returncode: int):
    rows = []
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            continue
    finals = [row for row in rows if row.get('type') == 'result']
    final = finals[-1] if finals else {}
    notice = (final.get('notice') or {}).get('text') or ''
    data = final.get('data') or {}
    fids = data.get('fids') or []
    # Both the final service result and remote file identity are needed for success.
    if returncode == 0 and final.get('code') == 0 and len(fids) == 1 and data.get('successCount') == 1 and not data.get('failedCount'):
        return {'state': 'succeeded', 'remote_fid': str(fids[0]), 'notice': notice, 'error': None}
    return {'state': 'needs_review', 'notice': notice,
            'error': str(final.get('msg') or '未收到明确上传结果，请核对网盘后处理')}


def process_one(queue: Queue, config: dict, uploader=None):
    task = queue.claim()
    if not task:
        return None
    if task['state'] != 'preparing':
        return queue.finish_cancel(task['id'])
    owned = uploader is None
    try:
        if owned:
            try:
                from uploader_factory import create_uploader
                uploader = create_uploader(config)
            except Exception:
                return queue.preparation_failed(task['id'], 'setup_required', '上传服务配置不可用，请保留任务并检查配置')
        return _process_preparing(queue, config, uploader, task)
    finally:
        if owned and hasattr(uploader, 'close'):
            try:
                uploader.close()
            except Exception:
                pass  # Resource cleanup cannot change the recorded task outcome.


def _process_preparing(queue, config, uploader, task):
    try:
        check_free(queue.root, config)
        uploader.preflight()
    except Exception as exc:
        return queue.preparation_failed(task['id'], 'setup_required', str(exc))
    notice = ''
    try:
        local = prepare(task, queue.root, config,
                        cancel_check=lambda: queue.get(task['id'])['state'] == 'cancel_requested')
        checksum = sha256(local)
        notice = uploader.check_destination(local, task,
                    cancel_check=lambda: queue.get(task['id'])['state'] == 'cancel_requested')
        queue.db.execute("UPDATE tasks SET notice=? WHERE id=?", (notice or '', task['id']))
        queue.db.commit()
        task = queue.start_upload(task, local, checksum)
    except DestinationUnchecked as exc:
        state = 'setup_required' if isinstance(exc, AgentEnvironmentUnsupported) else ('name_conflict' if isinstance(exc, NameConflict) else 'destination_unchecked')
        return queue.preparation_failed(task['id'], state, str(exc), exc.notice or notice or '')
    except Exception as exc:
        return queue.preparation_failed(task['id'], 'prepare_failed', str(exc), notice or '')
    try:
        result = uploader.upload(local, task)
        if task.get('notice'):
            result['notice'] = '\n\n'.join(dict.fromkeys(filter(None, [task['notice'], result.get('notice')])))
        queue.update(task['id'], **result)
    except Exception:
        queue.update(task['id'], 'needs_review', error='上传调用中断，结果未知，请先核对网盘，不能直接重传')
    if queue.get(task['id'])['state'] == 'needs_review':
        queue.review_upload(task['id'])
    return queue.get(task['id'])


def main():
    parser = argparse.ArgumentParser(description='视频＋点评 → 夸克分类收藏')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    sub = parser.add_subparsers(dest='action', required=True)
    add = sub.add_parser('submit')
    add.add_argument('--source', required=True)
    add.add_argument('--comment', required=True)
    add.add_argument('--category', required=True)
    add.add_argument('--request-key')
    collect = sub.add_parser('collect', help='提交视频来源与包含 #类目 的点评')
    collect.add_argument('--source', required=True)
    collect.add_argument('--note', required=True)
    collect.add_argument('--request-key')
    sub.add_parser('list')
    sub.add_parser('run')
    retry = sub.add_parser('retry')
    retry.add_argument('task_id')
    options = parser.parse_args()
    from runtime_config import load_runtime_config
    config = load_runtime_config(options.config)
    root = Path(config.get('data_dir', 'data'))
    if not root.is_absolute():
        root = options.config.resolve().parent / root
    queue = Queue(root)
    try:
        if options.action == 'submit':
            print(queue.submit(options.source, options.comment, options.category, config.get('categories', {}), options.request_key))
        elif options.action == 'collect':
            print(queue.submit_note(options.source, options.note, config.get('categories', {}), options.request_key))
        elif options.action == 'list':
            print(json.dumps(queue.all(), ensure_ascii=False, indent=2))
        elif options.action == 'retry':
            queue.retry(options.task_id)
        elif options.action == 'run':
            from execution_coordination import execution_entry
            with execution_entry(options.config, config, 'worker', queue.root), WorkerLock(queue.root):
                queue.recover()
                while True:
                    result = process_one(queue, config)
                    if result is None:
                        break
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    if result.get('notice'):
                        print(result['notice'], flush=True)
    finally:
        queue.db.close()


if __name__ == '__main__':
    main()
