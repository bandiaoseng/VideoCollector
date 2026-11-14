"""One execution entry at a time, with durable cross-entry unknown-upload checks."""
from contextlib import contextmanager, closing
import json
from pathlib import Path
import re
import sqlite3


def coordination_root(config_path, config):
    explicit = config.get('coordination_dir')
    if explicit is not None:
        if not isinstance(explicit, str) or not Path(explicit).is_absolute():
            raise ValueError('执行协调目录必须是绝对路径。')
        return Path(explicit)
    settings = config.get('quark_web')
    session = settings.get('session_file') if isinstance(settings, dict) else None
    if isinstance(session, str) and Path(session).is_absolute():
        return Path(session).parent / 'collector-coordination'
    return Path(config_path).resolve().parent / '.runtime' / 'execution-coordination'


def unresolved(root):
    """Read-only inspection: never recover, modify or execute another queue."""
    from upload_evidence import confirmed_upload
    db_path = root / 'tasks.sqlite'
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError('另一入口的任务记录无法核对，已停止切换；请保留原数据。')
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT * FROM tasks WHERE state IN ('uploading','needs_review')")]
        for task in rows:
            try:
                confirmed_upload(root, task)
            except (ValueError, OSError):
                return True
        return False
    except sqlite3.Error:
        raise ValueError('另一入口的任务记录暂时无法读取，未开始新的解析或上传。') from None


def check_entry(entry):
    root = Path(entry['root'])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('已登记的任务目录缺失或异常，不能忽略原上传记录。')
    if entry['mode'] == 'desktop':
        queues = [p for p in root.iterdir() if re.fullmatch('[0-9a-f]{64}', p.name)]
    else:
        queues = [root]
    for queue in queues:
        if queue.is_symlink() or unresolved(queue):
            raise ValueError('另一入口还有上传结果待核对，请先从原入口核对后再切换；本次未开始解析或上传。')


@contextmanager
def execution_entry(config_path, config, mode, queue_root):
    from collector import WorkerLock
    from quark_web import save_private
    if mode not in {'desktop', 'worker'}:
        raise ValueError('执行入口无效')
    home = coordination_root(config_path, config)
    home.mkdir(parents=True, exist_ok=True)
    # This lock is distinct from each queue's lock; it is held for a worker's
    # lifetime, and only for an explicitly started desktop operation.
    with WorkerLock(home):
        path = home / 'entries.json'
        try:
            entries = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
            if not isinstance(entries, list) or any(
                    not isinstance(e, dict) or e.get('mode') not in {'desktop','worker'}
                    or not isinstance(e.get('root'), str) for e in entries):
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError('执行入口登记损坏，已停止；请保留原记录。') from None
        # Discover historical queues before either upgraded entry runs first.
        desktop = Path(config_path).resolve().parent / '.runtime' / 'desktop-jobs'
        data = Path(config.get('data_dir', 'data'))
        if not data.is_absolute(): data = Path(config_path).resolve().parent / data
        for kind, root in [('desktop', desktop), ('worker', data)]:
            item = {'mode':kind, 'root':str(root.resolve())}
            if root.is_dir() and (kind=='desktop' or (root/'tasks.sqlite').exists()) and item not in entries:
                entries.append(item)
        current = {'mode':mode, 'root':str(Path(queue_root).resolve())}
        if current not in entries: entries.append(current)
        # Persist discovery even if switching is refused, preventing a later
        # config edit from silently hiding the unresolved queue.
        save_private(path, entries)
        for entry in entries:
            if entry != current:
                check_entry(entry)
        yield
