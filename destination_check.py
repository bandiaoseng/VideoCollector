"""Complete directory preflight; NOT an atomic remote create-if-absent."""
import json
from pathlib import Path
import time
import unicodedata
import uuid
from process_runner import run_managed


class DestinationUnchecked(ValueError):
    def __init__(self, message='无法完整检查目标文件夹，尚未上传，请稍后重试', notice=''):
        super().__init__(message)
        self.notice = notice


class NameConflict(DestinationUnchecked):
    def __init__(self, notice=''):
        super().__init__('目标文件夹已有同名文件或待核对的同名上传，尚未上传；请保留原记录，用不同点评重新提交', notice)


class AgentEnvironmentUnsupported(DestinationUnchecked):
    def __init__(self, notice=''):
        super().__init__('夸克上传组件拒绝当前运行环境（错误码 -104）：无法识别当前 Agent 环境，禁止继续使用。'
                         '这不是文件夹错误；已下载的视频和点评已保留。需修正上传组件接入，反复重试不能解决。', notice)


def is_environment_refusal(row):
    return (isinstance(row, dict) and row.get('type') == 'result'
            and row.get('action') == 'runtime' and row.get('code') == -104)


def cached_environment_refusal(folder):
    """Read known prior refusals only; no CLI calls or credential access."""
    for path in (Path(folder) / 'destination-checks').glob('*/stdout.ndjson'):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            if any(is_environment_refusal(json.loads(line)) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()):
                return True
        except (OSError, ValueError):
            continue
    return False


def name_key(name):
    return unicodedata.normalize('NFC', name).casefold()


def fid_key(fid):
    return fid.rsplit('|', 1)[-1] if '|' in fid and fid.rsplit('|', 1)[-1] else fid


def inspect_result(stdout, returncode, directory, task, filename):
    notice = ''
    try:
        rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('Invalid output')
        finals = [row for row in rows if row.get('type') == 'result']
        if finals:
            notice = (finals[-1].get('notice') or {}).get('text') or ''
        if not isinstance(notice, str) or len(finals) != 1:
            raise ValueError('Invalid result')
        final = finals[0]
        if is_environment_refusal(final):
            raise AgentEnvironmentUnsupported(notice)
        if returncode != 0 or type(final.get('code')) is not int or final['code'] != 0 or final.get('action') != 'browse':
            raise ValueError('Browse failed')
        data = final['data']
        if data.get('hasMore') is not False or fid_key(data['parent_fid']) != fid_key(task['parent_fid']):
            raise ValueError('Incomplete/wrong directory')
        artifact = Path(data['file_path'])
        if not artifact.is_absolute() or not artifact.resolve().is_relative_to(directory.resolve()) or not artifact.is_file():
            raise ValueError('Invalid manifest')
        entries = [json.loads(line) for line in artifact.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if type(data.get('count')) is not int or data['count'] != len(entries):
            raise ValueError('Truncated manifest')
        for entry in entries:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k] for k in ('fid', 'filename')):
                raise ValueError('Unusable entry')
            if 'parent_fid' in entry and fid_key(entry['parent_fid']) != fid_key(task['parent_fid']):
                raise ValueError('Unexpected parent')
        proof = {'version': 1, 'task_id': task['id'], 'parent_fid': task['parent_fid'],
                 'filename': filename, 'checked_at': time.time(), 'entries': len(entries),
                 'conflict': any(name_key(row['filename']) == name_key(filename) for row in entries)}
        (directory / 'check.json').write_text(json.dumps(proof, ensure_ascii=False), encoding='utf-8')
        if proof['conflict']:
            raise NameConflict(notice)
        return notice
    except DestinationUnchecked:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        raise DestinationUnchecked(notice=notice if isinstance(notice, str) else '') from None


def check_destination(config, path, task, cancel_check=None):
    directory = path.parent / 'destination-checks' / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    session = config.get('quark_session_id') or f'{int(time.time())}-{uuid.uuid4().hex[:6]}'
    args = [config.get('node', 'node'), config['quark_cli'], 'browse', '--all',
            '--parent-fid', task['parent_fid'], '--task-dir', str(directory),
            '--session-id', session, '--session-input', task.get('input_text') or task['comment']]
    try:
        result = run_managed(args, capture_output=True, stdout_path=directory / 'stdout.ndjson',
                             timeout=config.get('destination_check_timeout', 120), cancel_check=cancel_check)
    except Exception:
        raise DestinationUnchecked() from None
    return inspect_result(result.stdout, result.returncode, directory, task, path.name)
