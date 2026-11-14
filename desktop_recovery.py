"""Desktop task identity and explicit, local-only conflict recovery.

Call mutations under the desktop root's WorkerLock. No network operation is
defined here. Reuse markers are a persistent prohibition on fresh downloading.
"""
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import closing

from collector import prepared_file, sha256, video_name
from destination_check import fid_key, name_key
from share_input import parse_share

JOB = re.compile(r'[0-9a-f]{64}')
TASK = re.compile(r'[0-9a-f]{32}')
RECOVERABLE = {'queued', 'prepare_failed', 'setup_required', 'destination_unchecked'}


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _base(root):
    root = Path(root).absolute()
    if _linked(root) or not root.is_dir():
        raise ValueError('桌面任务目录异常，已停止，未重新解析。')
    return root.resolve()


def _job(root, key):
    if not isinstance(key, str) or not JOB.fullmatch(key):
        raise ValueError('任务目录编号无效，未重新解析。')
    path = _base(root) / key
    if _linked(path) or (path.exists() and not path.is_dir()):
        raise ValueError('任务目录不能是链接或普通文件。')
    return path


def _current(root, path):
    path = Path(path).absolute()
    expected = _job(root, path.name)
    if path.parent.resolve() != _base(root) or path != expected:
        raise ValueError('任务必须位于桌面任务目录内。')
    return expected


def _file(folder, name):
    path = folder / name
    if _linked(path) or (path.exists() and not path.is_file()):
        raise ValueError('任务记录或缓存路径异常，已停止。')
    return path


def _folder(job, ident, create=False):
    if not isinstance(ident, str) or not TASK.fullmatch(ident):
        raise ValueError('任务编号无效，已停止。')
    path = job
    for part in ('files', ident):
        path = path / part
        if _linked(path) or (path.exists() and not path.is_dir()):
            raise ValueError('任务缓存目录异常，已停止。')
        if create:
            path.mkdir(exist_ok=True)
    return path


def _db_path(job):
    path = _file(job, 'tasks.sqlite')
    if not path.is_file():
        raise ValueError('已有任务缺少数据库，已停止，未重新解析。')
    for suffix in ('-wal', '-shm'):
        _file(job, 'tasks.sqlite' + suffix)
    return path


def _identity(source, comment, parent):
    if not isinstance(comment, str) or not comment.strip() or not isinstance(parent, str) or not parent.strip():
        raise ValueError('已有任务身份记录不完整，未重新解析。')
    return parse_share(source)['url'], comment.strip(), fid_key(parent.strip())


def _rows(job):
    try:
        with closing(sqlite3.connect(_db_path(job).as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute('SELECT * FROM tasks')]
        for row in rows:
            if not TASK.fullmatch(row.get('id', '')) or not isinstance(row.get('state'), str):
                raise ValueError('已有任务记录损坏，未重新解析。')
            _identity(row['source'], row['comment'], row['parent_fid'])
        return rows
    except (sqlite3.Error, OSError, KeyError, TypeError) as exc:
        raise ValueError('无法完整读取已有任务，已停止，未重新解析。') from None


def _jobs(root):
    for path in sorted(_base(root).iterdir()):
        if JOB.fullmatch(path.name):
            yield _job(root, path.name)


def resolve_job(root, plan, default_key):
    """Retain old raw-note keys while recognizing equivalent parsed input."""
    direct = _job(root, default_key)
    if direct.exists():
        return direct
    expected = _identity(plan['url'], plan['comment'], plan['parent_fid'])
    matches = []
    for job in _jobs(root):
        for task in _rows(job):
            if _identity(task['source'], task['comment'], task['parent_fid']) == expected:
                matches.append(job)
    if len(matches) > 1:
        raise ValueError('找到多条相同输入的历史任务，请先核对原任务，未重新解析。')
    return matches[0] if matches else direct


def guard_pending_uploads(root, current_job, plan):
    """Unknown uploads reserve their source and destination across databases."""
    current_job = _current(root, current_job)
    source, _, parent = _identity(plan['url'], plan['comment'], plan['parent_fid'])
    filename = name_key(video_name(plan['comment'], '.mp4'))
    for job in _jobs(root):
        if job == current_job:
            continue
        for task in _rows(job):
            if task['state'] not in {'uploading', 'needs_review'}:
                continue
            old_source, _, old_parent = _identity(task['source'], task['comment'], task['parent_fid'])
            old_name = video_name(task['comment'], '.mp4')
            if old_source == source or (old_parent == parent and name_key(old_name) == filename):
                raise ValueError(f'“{old_name}”还有未核对的上传（任务 {task["id"]}），请先核对原任务；本次未开始新的解析或上传。')


def _read_json(path):
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise ValueError('缓存续接记录损坏，已停止，未重新解析。') from None


def _write_json(path, data):
    temporary = _file(path.parent, path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_forks(root, reference, target):
    # Covers a crash after a target marker but before the source reservation.
    for job in _jobs(root):
        files = job / 'files'
        if _linked(files):
            raise ValueError('历史任务缓存目录异常，已停止。')
        if not files.exists():
            continue
        for item in files.iterdir():
            if not TASK.fullmatch(item.name):
                continue
            folder = _folder(job, item.name)
            marker = _file(folder, 'desktop-reuse.json')
            if marker.exists():
                data = _read_json(marker)
                if data.get('reference') == reference and data.get('target') != target:
                    raise ValueError('原任务已有另一条修改记录，请继续原修改记录，不能重复分叉。')


def _supersede(job, ident, identity):
    with closing(sqlite3.connect(_db_path(job), timeout=2)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
        if row is None or row['state'] not in {'name_conflict', 'superseded'} or _identity(
                row['source'], row['comment'], row['parent_fid']) != identity:
            raise ValueError('原任务状态已变化，已停止修改。')
        db.execute("UPDATE tasks SET state='superseded',updated=? WHERE id=?", (time.time(), ident))


def prepare_cached_revision(root, queue, task, plan, reference):
    """Bind one conflict revision to verified cached bytes, never a downloader."""
    if not isinstance(reference, dict) or set(reference) != {'job_key', 'task_id'}:
        raise ValueError('缺少明确的原任务引用，未重新解析。')
    source_job = _job(root, reference['job_key'])
    source_folder = _folder(source_job, reference['task_id'])
    target_job = _current(root, queue.root)
    if source_job == target_job:
        raise ValueError('修改任务必须保留独立的原任务记录。')
    if not isinstance(task, dict) or not TASK.fullmatch(task.get('id', '')):
        raise ValueError('修改任务编号无效。')
    task = queue.get(task['id'])
    if task['state'] not in RECOVERABLE:
        raise ValueError('此任务已完成、正在处理或需要核对，不能修改缓存或重新上传。')
    expected = _identity(plan['url'], plan['comment'], plan['parent_fid'])
    if _identity(task['source'], task['comment'], task['parent_fid']) != expected:
        raise ValueError('修改输入与任务不一致，已停止。')
    filename = video_name(task['comment'], '.mp4')
    if plan.get('filename', filename) != filename:
        raise ValueError('目标文件名与点评不一致。')
    rows = [row for row in _rows(source_job) if row['id'] == reference['task_id']]
    if len(rows) != 1:
        raise ValueError('原任务不存在或不唯一，未重新解析。')
    original = rows[0]
    original_identity = _identity(original['source'], original['comment'], original['parent_fid'])
    if original['state'] not in {'name_conflict', 'superseded'}:
        raise ValueError('只能修改尚未上传的同名冲突任务；未知上传必须先核对。')
    if original_identity[0] != expected[0]:
        raise ValueError('修改时不能更换视频来源，未重新解析。')
    if (name_key(video_name(original['comment'], '.mp4')) == name_key(filename)
            and original_identity[2] == expected[2]):
        raise ValueError('文件名和目标目录没有改变，请修改点评或类目。')
    target = {'job_key': target_job.name, 'task_id': task['id']}
    reservation_path = _file(source_folder, 'desktop-revision.json')
    reservation = {'version': 1, 'reference': reference, 'target': target}
    if reservation_path.exists():
        if _read_json(reservation_path) != reservation:
            raise ValueError('原任务已有另一条修改记录，请继续原修改记录。')
    elif original['state'] == 'superseded':
        raise ValueError('原修改记录缺失，不能重新分叉或解析。')
    _check_forks(root, reference, target)
    target_folder = _folder(target_job, task['id'], create=True)
    marker_path = _file(target_folder, 'desktop-reuse.json')
    identity = {'version': 1, 'reference': reference, 'target': target,
                'source': expected[0], 'comment': task['comment'],
                'parent_fid': task['parent_fid'], 'filename': filename}
    marker = _read_json(marker_path) if marker_path.exists() else None
    if marker is not None:
        if any(marker.get(k) != v for k, v in identity.items()):
            raise ValueError('修改任务的缓存绑定不一致，未重新解析。')
        if marker.get('verification') not in {'pending', 'verified'}:
            raise ValueError('修改任务的缓存校验状态无效，未重新解析。')
        if marker.get('verification') == 'pending':
            if 'bytes' in marker or 'sha256' in marker:
                raise ValueError('未校验的缓存记录包含冲突数据，未重新解析。')
        elif (type(marker.get('bytes')) is not int or marker['bytes'] <= 0
                or not isinstance(marker.get('sha256'), str) or not JOB.fullmatch(marker['sha256'])):
            raise ValueError('修改任务的缓存绑定不一致，未重新解析。')
    else:
        # Persist the local-only intent even when source validation fails. A
        # later ordinary Start must never turn a broken cache into a paid parse.
        marker = {**identity, 'verification': 'pending'}
        _write_json(marker_path, marker)
    _file(target_folder, 'prepared.json')
    target_file = _file(target_folder, filename)
    existing = prepared_file(task, target_folder)
    if existing is not None and marker.get('verification') != 'pending':
        if existing.stat().st_size != marker['bytes'] or sha256(existing) != marker['sha256']:
            raise ValueError('修改任务的缓存已改变，未重新解析。')
        if not reservation_path.exists():
            _write_json(reservation_path, reservation)
        _supersede(source_job, original['id'], original_identity)
        return existing
    if (target_folder / 'prepared.json').exists():
        raise ValueError('修改任务的已有缓存校验失败，未重新解析。')
    _file(source_folder, 'prepared.json')
    _file(source_folder, video_name(original['comment'], '.mp4'))
    source_file = prepared_file(original, source_folder)
    if source_file is None:
        raise ValueError('原任务的已下载视频缺失或损坏，已停止，未重新解析。')
    verified = {'bytes': source_file.stat().st_size, 'sha256': sha256(source_file)}
    if marker.get('verification') != 'pending' and any(marker[k] != v for k, v in verified.items()):
        raise ValueError('原视频与续接记录不符，未重新解析。')
    if marker.get('verification') == 'pending':
        marker = {**identity, 'verification': 'verified', **verified}
        _write_json(marker_path, marker)
    if not reservation_path.exists():
        _write_json(reservation_path, reservation)
    if target_file.exists():
        if target_file.stat().st_size != verified['bytes'] or sha256(target_file) != verified['sha256']:
            raise ValueError('修改任务的缓存损坏或被占用，未覆盖、未重新解析。')
    else:
        temporary = _file(target_folder, 'desktop-copy-' + uuid.uuid4().hex + '.part')
        try:
            shutil.copyfile(source_file, temporary)
            if temporary.stat().st_size != verified['bytes'] or sha256(temporary) != verified['sha256']:
                raise ValueError('本地视频复制校验失败，未重新解析。')
            temporary.replace(target_file)
        finally:
            temporary.unlink(missing_ok=True)
    receipt = {'version': 1, 'task': {k: task[k] for k in ('id', 'source', 'comment')},
               'filename': filename, **verified}
    _write_json(target_folder / 'prepared.json', receipt)
    _supersede(source_job, original['id'], original_identity)
    return target_file
