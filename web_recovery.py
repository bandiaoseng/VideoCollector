"""Explicit read-only upload review, serialized per task independently of worker."""
import re

from collector import WorkerLock, prepared_file
from quark_web_uploader import WebUploader


def review_upload(queue, task_id, config, confirmation=None):
    if not isinstance(task_id, str) or not re.fullmatch('[0-9a-f]{32}', task_id):
        raise ValueError('任务编号无效')
    folder = queue.root / 'review-locks' / task_id
    folder.mkdir(parents=True, exist_ok=True)
    with WorkerLock(folder):
        task = queue.get(task_id)
        if task['state'] == 'succeeded':
            return {}
        if task['state'] != 'needs_review':
            raise ValueError('当前任务不需要核对上传结果')
        task = queue.review_upload(task_id)
        if task['state'] == 'succeeded' or 'quark_web' not in config:
            return {}
        local = prepared_file(task, queue.root / 'files' / task_id)
        if local is None:
            raise ValueError('原视频缺失或校验不符，请保留任务；不会重新下载或上传。')
        uploader = WebUploader(config)
        try:
            result = uploader.review(local, task, confirmation=confirmation)
        finally:
            uploader.close()
        if result['state'] == 'succeeded':
            queue.review_upload(task_id)
            return {}
        message = result.get('error') or result.get('notice') or '上传结果仍待核对，请勿重传。'
        queue.db.execute("UPDATE tasks SET error=?,review_note=? WHERE id=? AND state='needs_review'",
                         (message, message, task_id))
        queue.db.commit()
        extra = {}
        if result.get('review_candidate') and result.get('review_confirmation'):
            # Keep remote IDs, local paths and underlying evidence off the phone.
            extra['review_candidate'] = {key: result['review_candidate'][key]
                                         for key in ('filename', 'full_path', 'bytes')}
            extra['review_confirmation'] = result['review_confirmation']
        return extra
