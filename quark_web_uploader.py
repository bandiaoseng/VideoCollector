"""Task-bound standalone uploader, with independent web login and receipts."""
import hashlib
import json
from pathlib import Path
import re
import time

from collector import sha256, SetupRequired
from destination_check import DestinationUnchecked, NameConflict, name_key
from quark_web import WebClient, WebError, save_private, close_client
from upload_evidence import evidence_directory, confirmed_upload, review_token, snapshot


class WebUploader:
    def __init__(self, config, progress=lambda text: None, *, client=None):
        self.config = config
        self.progress = progress
        settings = config.get('quark_web')
        self.settings = settings if isinstance(settings, dict) else {}
        # Failed/corrupt login must become a resumable setup_required task,
        # rather than raising before process_one can record the failure.
        self.client = client

    def _ensure_client(self):
        if self.client is None:
            try:
                session = self.settings.get('session_file')
                if not isinstance(session, str) or not Path(session).is_file():
                    raise SetupRequired('请先点击“登录夸克”，用手机夸克扫码一次；已有视频无需重新下载。')
                self.client = WebClient(session)
            except WebError as exc:
                raise SetupRequired(str(exc)) from None
            except (OSError, TypeError, KeyError, AttributeError, ValueError):
                raise SetupRequired('本机夸克登录记录不可用，请重新扫码登录；原任务和视频已保留。') from None
        return self.client

    def close(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                close_client(client)
            except Exception:
                # Transport cleanup must not replace the recorded task result,
                # or hide the original failure while unwinding a finally block.
                pass

    def preflight(self):
        session = self.settings.get('session_file')
        if not isinstance(session, str) or not Path(session).is_file():
            raise SetupRequired('请先点击“登录夸克”，用手机夸克扫码一次；已有视频无需重新下载。')
        self.progress('正在检查夸克登录…')
        try:
            self._ensure_client().list_all('0')
        except WebError as exc:
            raise SetupRequired(str(exc)) from None

    def check_destination(self, path, task, cancel_check=None):
        self.progress('正在核对夸克目标文件夹…')
        parts = (self.settings.get('category_paths') or {}).get(task['category'])
        try:
            parent = self.client.resolve(parts)
            rows = self.client.list_all(parent)
        except (WebError, TypeError) as exc:
            raise DestinationUnchecked(str(exc)) from None
        if any(name_key(r['file_name']) == name_key(path.name) for r in rows):
            raise NameConflict()
        save_private(path.parent / 'web-destination.json', {'task_id': task['id'],
            'configured_parent': task['parent_fid'], 'web_parent': parent, 'path': parts,
            'filename': path.name, 'checked_at': time.time(), 'conflict': False})
        return ''

    def upload(self, path, task):
        try:
            return self._upload(path, task)
        except Exception as exc:
            message = str(exc) if isinstance(exc, WebError) else '上传回执尚未完整核对，请勿重复上传。'
            save_private(evidence_directory(path.parent.parent.parent, task) / 'web-error.json',
                         {'error_type': type(exc).__name__, 'message': message})
            return {'state': 'needs_review', 'error': message, 'notice': ''}

    def review(self, path, task, confirmation=None):
        """Resolve an ambiguous upload using reads only; never authorize retry.

        Hash-poor listings require an explicit user decision about the exact
        previously displayed candidate. A fresh listing must still match it.
        """
        if task['state'] != 'needs_review':
            raise ValueError('当前任务无需核对上传结果。')
        path = Path(path)
        result = {'state': 'needs_review', 'error': None, 'notice': ''}
        try:
            root = path.parent.parent.parent
            directory = evidence_directory(root, task)
            if (path.parent.resolve() != (root / 'files' / task['id']).resolve()
                    or path.resolve() != Path(task['local_file']).resolve()
                    or path.is_symlink() or not path.is_file()):
                raise ValueError('找不到属于本任务的原视频，无法可靠核对；请保留任务，勿重复上传。')
            expected = json.loads(task['upload_snapshot'])
            hashes = {key: hashlib.new(key) for key in ('md5', 'sha1', 'sha256')}
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    for value in hashes.values(): value.update(block)
            hashes = {key: value.hexdigest() for key, value in hashes.items()}
            if expected != snapshot(task, path, hashes['sha256']) or hashes['sha256'] != task['file_hash']:
                raise ValueError('本地文件或任务记录已变化，无法确认原上传结果。')
            binding_path = path.parent / 'web-destination.json'
            if binding_path.is_symlink() or binding_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('原上传目录记录无效，无法可靠核对。')
            binding = json.loads(binding_path.read_text(encoding='utf-8'))
            parts = binding.get('path')
            if (binding.get('task_id') != task['id'] or binding.get('configured_parent') != task['parent_fid']
                    or binding.get('filename') != path.name or binding.get('conflict') is not False
                    or not isinstance(binding.get('web_parent'), str) or not binding['web_parent']
                    or not isinstance(parts, list) or not parts
                    or not all(isinstance(p, str) and p and '/' not in p for p in parts)):
                raise ValueError('原上传目录记录不完整，不能改投其他文件夹或重复上传。')
            self.progress('正在只读核对原夸克目录，不下载或上传视频…')
            rows = self._ensure_client().list_all(binding['web_parent'])
            matches = [row for row in rows if name_key(row.get('file_name', '')) == name_key(path.name)]
            if len(matches) != 1:
                raise ValueError('尚未找到唯一匹配文件，上传结果仍待核对；这不代表未上传，请勿重传。')
            found = matches[0]
            if (found.get('file_name') != path.name or found.get('file') is not True
                    or type(found.get('size')) is not int or found['size'] != expected['bytes']
                    or not isinstance(found.get('fid'), str) or not found['fid']):
                raise ValueError('云端文件名称、类型或大小不匹配，不能确认本次上传。')
            remote = {key: found.get(key) for key in ('fid', 'file_name', 'size', 'file')}
            for key, length in (('md5', 32), ('sha1', 40)):
                value = found.get(key)
                if value not in (None, ''):
                    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{%d}' % length, value):
                        raise ValueError('云端文件校验值格式异常，不能确认成功。')
                    remote[key] = value.lower()
                    if remote[key] != hashes[key]:
                        raise ValueError('云端文件校验值不匹配，不能用同名同大小代替内容核对。')
                else:
                    remote[key] = None
            # Retain available revision times in the confirmation identity.
            for key in ('updated_at', 'l_updated_at', 'modified_at'):
                value = found.get(key)
                if type(value) in (int, float, str): remote[key] = value
            candidate = {'version': 1, 'attempt': task['upload_attempt'], 'snapshot': expected,
                         'binding': binding, 'remote': remote, 'local_hashes': hashes}
            token = review_token(candidate)
            candidate_path = directory / 'web-review-candidate.json'
            method = 'remote_hash' if all(remote.get(key) == hashes[key] for key in ('md5', 'sha1')) else None
            if method is None and confirmation is not None:
                try:
                    if candidate_path.is_symlink() or candidate_path.stat().st_size > 2 * 1024 * 1024:
                        raise ValueError('无效候选记录')
                    previous = json.loads(candidate_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    previous = {}
                if (isinstance(confirmation, str) and confirmation == token
                        and previous.get('confirmation_token') == token and previous.get('candidate') == candidate):
                    method = 'user_confirmed'
            if method is None:
                save_private(candidate_path, {'candidate': candidate, 'confirmation_token': token,
                                              'checked_at': time.time()})
                return {**result, 'error': '已找到同名同大小文件，但云端未提供完整校验值。请在夸克确认内容和声音后，再明确标记完成；不会自动重传。'
                        if confirmation is None else '候选文件已变化或确认已失效，请重新核对下面的文件；没有标记成功，也没有重传。',
                        'review_candidate': {'filename': path.name, 'full_path': '夸克网盘/' + '/'.join(parts + [path.name]),
                                             'bytes': remote['size'], 'fid': remote['fid']},
                        'review_confirmation': token}
            proof = {'version': 1, 'provider': 'quark-web', 'method': method,
                     'candidate': candidate, 'candidate_token': token,
                     'user_confirmed': method == 'user_confirmed', 'checked_at': time.time(),
                     'full_path': '夸克网盘/' + '/'.join(parts)}
            save_private(directory / 'web-recovery.json', proof)
            verified = confirmed_upload(root, task)
            return {'state': 'succeeded', 'remote_fid': verified['remote_fid'], 'error': None,
                    'notice': verified['notice']}
        except (SetupRequired, WebError, ValueError) as exc:
            result['error'] = str(exc)
        except (OSError, KeyError, TypeError, AttributeError):
            result['error'] = '原任务记录不完整或暂时无法读取，上传结果仍待核对；请勿重复上传。'
        return result

    def _upload(self, path, task):
        binding = json.loads((path.parent / 'web-destination.json').read_text(encoding='utf-8'))
        if (binding['task_id'] != task['id'] or binding['configured_parent'] != task['parent_fid']
                or binding['filename'] != path.name or binding['conflict'] is not False
                or sha256(path) != task['file_hash']):
            raise ValueError('上传文件或目标发生变化，已停止。')
        self.progress('正在上传已保存的视频，不重新下载…')
        hashes = self.client.upload(path, binding['web_parent'], self.progress)
        if hashes['sha256'] != task['file_hash']:
            raise ValueError('文件在上传期间发生变化。')
        self.progress('上传请求已完成，正在核对夸克文件…')
        deadline = time.monotonic() + 30
        while True:
            candidates = [r for r in self.client.list_all(binding['web_parent']) if r['file_name'] == path.name]
            if candidates:
                break
            if time.monotonic() >= deadline:
                raise ValueError('尚未查到上传文件，需要核对，不能直接重传。')
            time.sleep(2)
        if len(candidates) != 1:
            raise ValueError('远端出现多个同名文件，需核对。')
        remote = candidates[0]
        if remote.get('file') is not True or remote.get('size') != path.stat().st_size:
            raise ValueError('远端文件类型或大小不匹配。')
        for key in ('md5', 'sha1'):
            if remote.get(key) and remote[key].lower() != hashes[key]:
                raise ValueError('远端文件校验值不匹配。')
        directory = evidence_directory(path.parent.parent.parent, task)
        receipt = {'provider': 'quark-web', 'attempt': task['upload_attempt'],
            'snapshot': json.loads(task['upload_snapshot']), 'upload_completed': True,
            'binding': binding, 'remote': {k: remote.get(k) for k in ('fid', 'file_name', 'size', 'file')},
            'sha256': hashes['sha256'], 'full_path': '夸克网盘/' + '/'.join(binding['path'])}
        save_private(directory / 'web-upload.json', receipt)
        proof = confirmed_upload(path.parent.parent.parent, task)
        return {'state': 'succeeded', 'remote_fid': proof['remote_fid'], 'error': None, 'notice': ''}
