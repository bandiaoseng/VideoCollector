"""Reconcile only complete, task-bound positive CLI receipts; never re-upload."""
import hashlib
import json
from pathlib import PureWindowsPath
import re


def evidence_directory(root, task):
    if not re.fullmatch(r'[0-9a-f]{32}', task['id']) or not re.fullmatch(r'[0-9a-f]{32}', task.get('upload_attempt') or ''):
        raise ValueError('缺少本次上传的记录，仍需核对网盘')
    return root / 'files' / task['id'] / 'upload-attempts' / task['upload_attempt']


def snapshot(task, local, checksum):
    return {'version': 1, 'task_id': task['id'], 'source': task['source'],
            'comment': task['comment'], 'parent_fid': task['parent_fid'],
            'filename': local.name, 'bytes': local.stat().st_size, 'sha256': checksum}


def review_token(candidate):
    """Bind an explicit user decision to the exact task and observed cloud file."""
    return hashlib.sha256(json.dumps(candidate, ensure_ascii=True,
                                     sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def recovery_proof(path, task, expected):
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('核对记录无效，仍需核对网盘')
    raw = path.read_bytes()
    proof = json.loads(raw)
    candidate = proof['candidate']
    binding, remote, hashes = candidate['binding'], candidate['remote'], candidate['local_hashes']
    original_binding = path.parents[2] / 'web-destination.json'
    if (original_binding.is_symlink() or original_binding.stat().st_size > 2 * 1024 * 1024
            or json.loads(original_binding.read_text(encoding='utf-8')) != binding):
        raise ValueError('核对目录与原上传目录不一致')
    parts = binding.get('path')
    if (proof.get('provider') != 'quark-web' or proof.get('version') != 1
            or candidate.get('attempt') != task['upload_attempt'] or candidate.get('snapshot') != expected
            or candidate.get('version') != 1
            or binding.get('task_id') != task['id'] or binding.get('configured_parent') != task['parent_fid']
            or binding.get('filename') != expected['filename'] or binding.get('conflict') is not False
            or not isinstance(binding.get('web_parent'), str) or not binding['web_parent']
            or not isinstance(parts, list) or not parts
            or not all(isinstance(p, str) and p and '/' not in p for p in parts)
            or not isinstance(remote.get('fid'), str) or not remote['fid']
            or remote.get('file') is not True or remote.get('file_name') != expected['filename']
            or type(remote.get('size')) is not int or remote['size'] != expected['bytes']
            or hashes.get('sha256') != expected['sha256']
            or any(not re.fullmatch(r'[0-9a-f]{%d}' % size, hashes.get(key) or '')
                   for key, size in (('md5', 32), ('sha1', 40)))
            or proof.get('candidate_token') != review_token(candidate)
            or proof.get('full_path') != '夸克网盘/' + '/'.join(parts)):
        raise ValueError('核对记录与原上传任务不匹配，仍需核对网盘')
    # A conflicting hash can never be overridden by a manual decision.
    for key in ('md5', 'sha1'):
        if remote.get(key) is not None and remote[key] != hashes[key]:
            raise ValueError('远端文件校验值不匹配，不能确认成功')
    method = proof.get('method')
    if method == 'remote_hash':
        if not all(remote.get(key) == hashes[key] for key in ('md5', 'sha1')):
            raise ValueError('缺少完整远端校验值，仍需人工核对')
        notice = '已只读核对云端文件的 MD5 和 SHA1，内容校验一致；没有重复上传。'
    elif method == 'user_confirmed':
        pending = path.with_name('web-review-candidate.json')
        if pending.is_symlink() or pending.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('缺少有效的用户核对记录')
        saved = json.loads(pending.read_text(encoding='utf-8'))
        if (proof.get('user_confirmed') is not True or saved.get('candidate') != candidate
                or saved.get('confirmation_token') != proof['candidate_token']):
            raise ValueError('用户确认与当前候选文件不匹配')
        notice = '已根据你对夸克文件内容的明确确认记录成功；远端未提供完整哈希，未冒充自动校验。没有重复上传。'
    else:
        raise ValueError('核对方式不明确，仍需核对网盘')
    return {'remote_fid': remote['fid'], 'notice': notice, 'full_path': proof['full_path'],
            'receipt_sha256': hashlib.sha256(raw).hexdigest()}


def completed_web_proof(path, task, expected):
    """The original successful upload receipt remains authoritative."""
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('上传回执无效，仍需核对网盘')
    raw = path.read_bytes()
    proof = json.loads(raw)
    remote, binding = proof['remote'], proof['binding']
    if (proof.get('provider') != 'quark-web' or proof.get('attempt') != task['upload_attempt']
            or proof.get('snapshot') != expected or proof.get('upload_completed') is not True
            or proof.get('sha256') != expected['sha256']
            or binding.get('task_id') != task['id'] or binding.get('configured_parent') != task['parent_fid']
            or not isinstance(binding.get('web_parent'), str) or not binding['web_parent']
            or binding.get('conflict') is not False or binding.get('filename') != expected['filename']
            or not isinstance(remote.get('fid'), str) or not remote['fid']
            or remote.get('file') is not True or remote.get('file_name') != expected['filename']
            or type(remote.get('size')) is not int or remote['size'] != expected['bytes']):
        raise ValueError('独立上传回执与任务不匹配，仍需核对网盘')
    result = {'remote_fid': remote['fid'], 'notice': '',
              'receipt_sha256': hashlib.sha256(raw).hexdigest()}
    # Older receipts may lack a directory path. Their existing positive upload
    # evidence remains valid, but an unbound display path must not be returned.
    if 'path' in binding:
        parts = binding['path']
        if (not isinstance(parts, list) or not parts
                or not all(isinstance(p, str) and p and '/' not in p for p in parts)
                or proof.get('full_path') != '夸克网盘/' + '/'.join(parts)):
            raise ValueError('上传回执中的保存路径与原目标目录不一致')
        result['full_path'] = proof['full_path']
    return result


def confirmed_upload(root, task):
    """Return evidence of the original upload, not a claim about current listing.

    No filename search, local SDK state or progress percentage can authorize a
    success or a retry here. A missing/invalid receipt stays unresolved.
    """
    directory = evidence_directory(root, task)
    try:
        expected = json.loads(task['upload_snapshot'])
        if (expected.get('version') != 1 or
            any(expected.get(key) != task[field] for key, field in
                [('task_id', 'id'), ('source', 'source'), ('comment', 'comment'), ('parent_fid', 'parent_fid'), ('sha256', 'file_hash')]) or
            expected.get('filename') != PureWindowsPath(task['local_file']).name or
            type(expected.get('bytes')) is not int or expected['bytes'] <= 0 or
            not re.fullmatch(r'[0-9a-f]{64}', expected.get('sha256') or '')):
            raise ValueError('上传记录与当前任务不一致，仍需核对网盘')
        recovery = directory / 'web-recovery.json'
        web_path = directory / 'web-upload.json'
        if web_path.exists():
            try:
                return completed_web_proof(web_path, task, expected)
            except (OSError, UnicodeError, TypeError, KeyError, AttributeError, ValueError):
                if not recovery.exists(): raise
        if recovery.exists():
            return recovery_proof(recovery, task, expected)
        output = directory / 'stdout.ndjson'
        if output.is_symlink() or output.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('上传回执无法完整核对，仍需核对网盘')
        raw = output.read_bytes()
        if not raw.endswith(b'\n'):
            raise ValueError('没有完整上传回执，仍需核对网盘')
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        if not rows or any(not isinstance(row, dict) or row.get('action') != 'upload' for row in rows):
            raise ValueError('上传回执格式不匹配，仍需核对网盘')
        finals = [r for r in rows if r.get('type') == 'result']
        items = [r for r in rows if r.get('type') == 'list']
        if len(finals) != 1 or len(items) != 1 or rows[-1] != finals[0]:
            raise ValueError('没有完整且唯一的文件上传回执，仍需核对网盘')
        final, item = finals[0], items[0]
        data, file = final.get('data'), item.get('data')
        if not isinstance(data, dict) or not isinstance(file, dict):
            raise ValueError('上传回执缺少文件信息，仍需核对网盘')
        fids = data.get('fids')
        if (type(final.get('code')) is not int or final['code'] != 0 or
            type(item.get('code')) is not int or item['code'] != 0 or
            type(data.get('successCount')) is not int or data['successCount'] != 1 or
            type(data.get('fileCount')) is not int or data['fileCount'] != 1 or
            type(data.get('failedCount', 0)) is not int or data.get('failedCount', 0) != 0 or
            not isinstance(fids, list) or len(fids) != 1 or not isinstance(fids[0], str) or not fids[0].strip() or
            data.get('fileNames') != [expected['filename']] or
            type(data.get('totalSize')) is not int or data['totalSize'] != expected['bytes'] or
            file.get('fileId') != fids[0] or file.get('fileName') != expected['filename'] or
            type(file.get('fileSize')) is not int or file['fileSize'] != expected['bytes']):
            raise ValueError('回执尚不能确认这个文件上传成功，仍需核对网盘')
        notice = final.get('notice') or {}
        if not isinstance(notice, dict) or not isinstance(notice.get('text', ''), str):
            raise ValueError('上传回执格式不完整，仍需核对网盘')
        result = {'remote_fid': fids[0], 'notice': notice.get('text', ''),
                  'receipt_sha256': hashlib.sha256(raw).hexdigest()}
        # Only take the legacy CLI display path from this validated final
        # receipt, never from a different or rejected proof file.
        full_path = data.get('fullPath')
        if isinstance(full_path, str) and full_path and not any(ord(c) < 32 for c in full_path):
            result['full_path'] = full_path
        return result
    except (OSError, UnicodeError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        raise ValueError('没有可用的完整上传回执，仍需核对网盘') from None
