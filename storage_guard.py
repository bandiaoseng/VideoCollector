"""Local disk admission and transfer checks; never remove task data."""
from pathlib import Path
import shutil


class StorageError(ValueError):
    pass


def policy(config):
    settings = config.get('storage', {})
    if not isinstance(settings, dict):
        raise StorageError('磁盘空间保护配置无效，任务已保留。')
    result = {}
    for key, default in [('reserve_bytes', 512 * 1024**2), ('download_budget_bytes', 1024**3)]:
        value = settings.get(key, default)
        if type(value) is not int or not 0 < value <= 1024**5:
            raise StorageError('磁盘空间保护配置无效，任务已保留。')
        result[key] = value
    return result


def budget(config, platform=None):
    amount = policy(config)['download_budget_bytes']
    providers = ['zhiling'] if platform == 'wechat' else ['dtk'] if platform == 'douyin' else ['zhiling', 'dtk']
    for name in providers:
        settings = config.get(name) or {}
        if not isinstance(settings, dict):
            raise StorageError('视频大小限制配置无效，任务已保留。')
        enabled = settings.get('enabled') is True if name == 'zhiling' else bool(settings.get('base_url'))
        if enabled:
            size = settings.get('max_bytes', 1024**3)
            if type(size) is not int or size <= 0:
                raise StorageError('视频大小限制配置无效，任务已保留。')
            amount = max(amount, size)
    return amount


def check_free(root, config, required=0):
    floor = policy(config)['reserve_bytes']
    path = Path(root).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        raise StorageError('暂时无法检查后台磁盘空间，任务已保留；未继续下载或上传。') from None
    if free < floor + required:
        raise StorageError('后台磁盘空间不足，任务和已有视频已保留；腾出空间后可继续，不会自动删除视频。')
    return free


def transfer_check(folder, config, cancel_check=None, *, limit=None):
    """Called by the managed process loop, or before each in-process write."""
    if cancel_check and cancel_check():
        return True
    check_free(folder, config)
    if limit is not None:
        # Only direct binary files; JSON receipts are retained separately. No
        # recursive scan of other tasks and no deletion of partial downloads.
        total = 0
        for path in Path(folder).iterdir():
            try:
                if path.is_file() and path.suffix.lower() not in {'.json', '.txt', '.ndjson'}:
                    total += path.stat().st_size
            except FileNotFoundError:
                # A downloader can atomically rename its completed stage file
                # between directory enumeration and stat; the final check runs
                # again after the child has exited.
                continue
        if total > limit:
            raise StorageError('视频下载占用已超过本任务的空间预算，已停止；原任务和解析记录已保留。')
    return False


def status(config, root):
    try:
        check_free(root, config, budget(config))
        return {'storage_ready': True, 'storage_problem': None}
    except (StorageError, TypeError, AttributeError):
        return {'storage_ready': False, 'storage_problem': '后台磁盘空间不足或空间保护配置无效，暂不能开始新的下载；原任务已保留。'}
