"""Explicit user-approved parser retry. No requests here; preserve each attempt."""
import hashlib
import json
from pathlib import Path
import uuid


def parser_recovery(folder, source):
    path = Path(folder) / 'download.zhiling-private.json'
    try:
        raw = path.read_bytes()
        cache = json.loads(raw)
        same = cache.get('source_hash') == hashlib.sha256(source.encode()).hexdigest()
        needed = same and cache.get('state') == 'request_started'
        return {'parse_retry_required': needed,
                'parse_retry_token': hashlib.sha256(raw).hexdigest() if needed else None,
                'parse_response_saved': same and cache.get('state') == 'received'}
    except (OSError, ValueError, AttributeError):
        return {'parse_retry_required': False, 'parse_response_saved': False}


def request_one_parse(queue, task_id, token, approved=False):
    """Persist consent and queue work; the downloader consumes it atomically.

    No marker is removed here. Repeated HTTP delivery of the same approval is
    idempotent even after the worker has started or finished that attempt.
    """
    from zhiling_download import private_json
    if approved is not True:
        raise ValueError('需要明确确认可能消耗一次解析额度。')
    queue.db.execute('BEGIN IMMEDIATE')
    try:
        task = queue.get(task_id)
        folder = queue.root / 'files' / task_id
        approval_path = folder / 'download.zhiling-approval.json'
        expected = {'version': 1, 'task_id': task_id, 'source_hash': hashlib.sha256(task['source'].encode()).hexdigest(),
                    'attempt_token': token}
        existing = None
        if approval_path.exists():
            existing = json.loads(approval_path.read_text(encoding='utf-8'))
        recovery = parser_recovery(folder, task['source'])
        if existing == expected and (task['state'] != 'prepare_failed' or
                                     recovery.get('parse_retry_token') != token):
            queue.db.commit()
            return
        if (task['state'] != 'prepare_failed' or task.get('prepared_from') is not None
                or not recovery['parse_retry_required'] or recovery.get('parse_retry_token') != token):
            raise ValueError('解析状态已变化，请刷新后再核对；没有新增解析请求。')
        private_json(approval_path, expected)
        queue.db.execute("UPDATE tasks SET state='queued',error=NULL WHERE id=? AND state='prepare_failed'", (task_id,))
        queue.db.commit()
    except BaseException:
        queue.db.rollback()
        raise


def approve_one_parse(queue, task, approved=False):
    if approved is not True or task['state'] != 'prepare_failed':
        raise ValueError('未确认重新解析，原任务未改变。')
    folder = queue.root / 'files' / task['id']
    if not parser_recovery(folder, task['source'])['parse_retry_required']:
        raise ValueError('当前任务不需要重新解析，未额外调用接口。')
    original = folder / 'download.zhiling-private.json'
    archived = folder / ('download.zhiling-private.attempt-' + uuid.uuid4().hex + '.json')
    # This function is called only under the desktop's exclusive worker lock.
    original.replace(archived)
    try:
        queue.retry(task['id'])
    except BaseException:
        archived.replace(original)
        raise
    return archived
