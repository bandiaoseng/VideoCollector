"""User-started desktop collector. Opening the window never starts a task."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import queue as messages
import threading
import time
import uuid
import subprocess
import sys

from collector import Queue, QuarkUploader, WorkerLock, process_one, video_name, prepared_file
from note_input import parse_note
from share_input import parse_share
from upload_evidence import confirmed_upload
from destination_check import AgentEnvironmentUnsupported, cached_environment_refusal
from parse_recovery import parser_recovery, approve_one_parse
from zhiling_download import check_parser_connection, ZhilingError
from desktop_recovery import resolve_job, guard_pending_uploads, prepare_cached_revision
from runtime_config import load_runtime_config, default_config_path
from execution_coordination import execution_entry

ROOT = Path(__file__).resolve().parent
RETRYABLE = {'prepare_failed', 'setup_required', 'destination_unchecked'}
LABELS = {
    'succeeded': '已存入夸克', 'prepare_failed': '下载或校验失败，未上传',
    'setup_required': '配置或授权需处理，未上传',
    'name_conflict': '夸克已有同名文件，未上传',
    'destination_unchecked': '目标目录未查清，未上传',
    'needs_review': '上传结果待核对，请勿重复提交',
    'queued': '已保存，等待处理', 'cancelled': '已取消',
    'superseded': '已按修改后的点评建立后续任务',
}


def load_config(path):
    try:
        return load_runtime_config(path)
    except (OSError, ValueError):
        raise ValueError('无法读取 config.json，请保留现有配置并反馈此提示。') from None


def plan_request(config, text, note):
    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
        raise ValueError('请填写视频分享链接，最多 10000 个字符。')
    if not isinstance(note, str) or len(note) > 4000:
        raise ValueError('点评最多 4000 个字符。')
    share = parse_share(text)
    comment, category = parse_note(note, config['categories'])
    return {**share, 'comment': comment, 'category': category,
            'filename': video_name(comment, '.mp4'),
            'parent_fid': config['categories'][category], 'note': note}


def job_key(plan):
    identity = [plan['url'], plan['note'], plan['parent_fid']]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class ProgressUploader(QuarkUploader):
    def __init__(self, config, progress):
        super().__init__(config)
        self.progress = progress

    def preflight(self):
        super().preflight()
        self.progress('正在解析、下载并校验视频，请保持窗口和电脑开启…')

    def check_destination(self, path, task, cancel_check=None):
        self.progress('视频已下载并校验，正在检查夸克目标目录…')
        return super().check_destination(path, task, cancel_check)

    def upload(self, path, task):
        self.progress('正在上传夸克，请等待明确结果…')
        return super().upload(path, task)


def run_request(config_path, text, note, *, retry=False, progress=lambda message: None,
                uploader_factory=None, require_cached=False, reparse=False,
                review=False, confirmation=None, revise_from=None):
    """Runs only this explicit request, never unrelated queues or pending jobs.

    The GUI calls this only after a user presses Start. Tests inject offline
    adapters. No activation is written back to the shared configuration.
    """
    config_path = Path(config_path).resolve()
    config = copy.deepcopy(load_config(config_path))
    plan = plan_request(config, text, note)
    if sum(bool(x) for x in (retry, reparse, review, revise_from)) > 1:
        raise ValueError('一次只能执行一种恢复操作。')
    if confirmation is not None and not review:
        raise ValueError('请先核对网盘中的候选文件。')
    if require_cached:
        config['cached_media_only'] = True
    standalone = 'quark_web' in config
    if uploader_factory is None:
        if standalone:
            from quark_web_uploader import WebUploader
            uploader_factory = WebUploader
        else:
            uploader_factory = ProgressUploader
    root = config_path.parent / '.runtime' / 'desktop-jobs'
    root.mkdir(parents=True, exist_ok=True)
    # One desktop upload at a time, even with multiple windows.
    with execution_entry(config_path, config, 'desktop', root), WorkerLock(root):
        job = resolve_job(root, plan, job_key(plan))
        key = job.name
        queue = Queue(job)
        reparse_archive = None
        try:
            queue.recover()
            task_id = queue.submit_note(plan['url'], note, config['categories'], key)
            if len(queue.all()) != 1:
                raise RuntimeError('任务记录异常，已停止，未启动新的上传。')
            task = queue.get(task_id)
            connection_failure = None
            review_result = {}
            intent_path = queue.root / 'files' / task_id / 'desktop-revision-intent.json'
            intent_identity = {name: task[name] for name in ('id', 'source', 'comment', 'parent_fid')}
            intent = None
            if intent_path.exists() or intent_path.is_symlink():
                try:
                    if intent_path.is_symlink() or intent_path.stat().st_size > 64 * 1024:
                        raise ValueError()
                    intent = json.loads(intent_path.read_text(encoding='utf-8'))
                    if intent.get('task') != intent_identity or 'reference' not in intent:
                        raise ValueError()
                except (OSError, ValueError, AttributeError, TypeError):
                    raise ValueError('原视频续接记录损坏，已停止；不会重新解析或下载。') from None
            if revise_from is not None:
                if intent is not None and intent['reference'] != revise_from:
                    raise ValueError('当前修改任务已绑定另一条原记录，不能更换视频。')
                intent = {'version': 1, 'task': intent_identity, 'reference': revise_from}
                # Save local-only intent before any guard/helper can fail. An
                # interrupted correction must never become a fresh paid parse.
                write_json(intent_path, intent)
                guard_pending_uploads(root, job, plan)
                prepare_cached_revision(root, queue, task, plan, revise_from)
                task = queue.get(task_id)
                if task['state'] in RETRYABLE:
                    queue.retry(task_id)
                    task = queue.get(task_id)
            reuse_path = queue.root / 'files' / task_id / 'desktop-reuse.json'
            if intent is not None or reuse_path.exists() or reuse_path.is_symlink():
                config['cached_media_only'] = True
                if (revise_from is None and task['state'] in (RETRYABLE | {'queued'})
                        and prepared_file(task, reuse_path.parent) is None):
                    try:
                        if reuse_path.is_symlink(): raise ValueError()
                        reference = (intent['reference'] if intent is not None else
                                     json.loads(reuse_path.read_text(encoding='utf-8'))['reference'])
                    except (OSError, ValueError, KeyError, TypeError):
                        raise ValueError('视频复用记录无法读取，已停止；不会重新解析或下载。') from None
                    guard_pending_uploads(root, job, plan)
                    prepare_cached_revision(root, queue, task, plan, reference)
                    task = queue.get(task_id)
            if require_cached and prepared_file(task, queue.root / 'files' / task_id) is None:
                raise ValueError('找不到经过校验的已有视频，已停止；本次不会重新下载。')
            if reparse:
                if config.get('cached_media_only'):
                    raise ValueError('仅使用已有视频时不能重新解析。')
                if task['state'] != 'prepare_failed' or not parser_recovery(
                        queue.root / 'files' / task_id, task['source'])['parse_retry_required']:
                    raise ValueError('当前任务不需要重新解析，未额外调用接口。')
                guard_pending_uploads(root, job, plan)
                progress('正在检查智凌连接，本步骤不调用解析接口…')
                try:
                    connection = check_parser_connection(config.get('zhiling'))
                except ZhilingError as exc:
                    connection_failure = exc
                    connection = {'parser_called': False, 'error': str(exc),
                                  'details': getattr(exc, 'details', None)}
                write_json(queue.root / 'files' / task_id / 'connection-check.json',
                           {**connection, 'checked_at': time.time()})
                if connection_failure is None:
                    reparse_archive = approve_one_parse(queue, task, approved=True)
                    task = queue.get(task_id)
            blocked = (not standalone and task['state'] in RETRYABLE and
                       cached_environment_refusal(queue.root / 'files' / task_id))
            if blocked:
                queue.update(task_id, 'setup_required', error=str(AgentEnvironmentUnsupported()))
                task = queue.get(task_id)
            if retry and task['state'] in RETRYABLE and not blocked and connection_failure is None:
                guard_pending_uploads(root, job, plan)
                queue.retry(task_id)
                task = queue.get(task_id)
            if review and task['state'] != 'succeeded':
                if task['state'] != 'needs_review' or not standalone:
                    raise ValueError('当前任务不需要核对独立夸克上传。')
                local = prepared_file(task, queue.root / 'files' / task_id)
                if local is None:
                    raise ValueError('本地视频缺失或校验不符，不能据此确认上传；原任务已保留，不会重新上传。')
                uploader = uploader_factory(config, progress)
                try:
                    review_result = uploader.review(local, task, confirmation=confirmation)
                    if review_result['state'] == 'succeeded':
                        task = queue.review_upload(task_id)
                    else:
                        queue.update(task_id, 'needs_review', error=review_result.get('error'),
                                     notice=review_result.get('notice', ''))
                        task = queue.get(task_id)
                finally:
                    if hasattr(uploader, 'close'): uploader.close()
            if task['state'] == 'queued':
                guard_pending_uploads(root, job, plan)
                # Enable only this explicitly requested run, not a background worker.
                if plan['platform'] == 'wechat':
                    config['zhiling'] = {**(config.get('zhiling') or {}), 'enabled': True}
                if not config.get('quark_session_id'):
                    session_file = root / 'session-id.txt'
                    if not session_file.exists():
                        session_file.write_text(f'{int(time.time())}-{uuid.uuid4().hex[:6]}', encoding='ascii')
                    config['quark_session_id'] = session_file.read_text(encoding='ascii').strip()
                progress('正在检查本机配置…')
                try:
                    uploader = uploader_factory(config, progress)
                except ValueError as exc:
                    queue.update(task_id, 'setup_required', error=str(exc))
                    task = queue.get(task_id)
                    uploader = None
                try:
                    if uploader is not None:
                        task = process_one(queue, config, uploader)
                finally:
                    if hasattr(uploader, 'close'): uploader.close()
            # If setup or the downloader's own connection check stopped before
            # parsing, restore the original ambiguity barrier. Ordinary Continue
            # must not turn this interrupted reparse into a later billed call.
            active_cache = queue.root / 'files' / task_id / 'download.zhiling-private.json'
            if reparse_archive is not None and not active_cache.exists():
                reparse_archive.replace(active_cache)
            report = {name: task.get(name) for name in (
                'id', 'state', 'comment', 'category', 'error', 'notice', 'review_note')}
            report.update(filename=plan['filename'], source=plan['url'],
                          job_key=key,
                          report_path=str(queue.root / 'desktop-result.json'), full_path='',
                          integration_blocked=blocked or (not standalone and task['state'] == 'setup_required' and
                              cached_environment_refusal(queue.root / 'files' / task_id)))
            if review_result:
                report['review_note'] = review_result.get('notice') or None
                for field in ('review_candidate', 'review_confirmation'):
                    if field in review_result: report[field] = review_result[field]
            recovery = parser_recovery(queue.root / 'files' / task_id, task['source'])
            report.update(recovery)
            if task['state'] == 'prepare_failed' and recovery['parse_retry_required']:
                report['error'] = ('智凌解析结果不明：没有收到回复，视频尚未开始下载。'
                    '本地无法确定这次是否消耗额度；可以点击“重新尝试解析”，确认后再请求一次。')
                detail = queue.root / 'files' / task_id / 'download.zhiling-error.json'
                try:
                    diagnostic = json.loads(detail.read_text(encoding='utf-8')).get('details')
                    if diagnostic and diagnostic.get('stage') == 'parse':
                        report['error_details'] = diagnostic
                        descriptions = {'dns':'无法找到服务地址', 'tls':'安全连接建立失败',
                                        'timeout':'等待回复超时', 'connection':'网络连接中断或无法建立'}
                        description = ('加密连接被中途关闭' if diagnostic.get('kind') == 'tls'
                                       and diagnostic.get('errno') == 8 else
                                       descriptions.get(diagnostic.get('kind'), '连接异常'))
                        report['error'] += '\n具体原因：' + description
                except (OSError, ValueError, AttributeError):
                    pass
            if task['state'] == 'prepare_failed':
                try:
                    detail = json.loads((queue.root / 'files' / task_id / 'download.zhiling-error.json').read_text(encoding='utf-8'))
                    if (detail.get('error') == task.get('error') and
                            (detail.get('details') or {}).get('stage') == 'preflight'):
                        report.update(error=task['error'], connection_check_failed=True,
                                      error_details=detail['details'])
                except (OSError, ValueError, AttributeError):
                    pass
            if connection_failure is not None:
                report.update(error=str(connection_failure), connection_check_failed=True,
                              error_details=getattr(connection_failure, 'details', None))
            if task['state'] == 'succeeded':
                # Full destination must come from the real service receipt, not a guess.
                try:
                    report['full_path'] = confirmed_upload(queue.root, task).get('full_path', '')
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            write_json(queue.root / 'desktop-result.json', report)
            return report
        finally:
            try:
                if reparse_archive is not None and reparse_archive.exists():
                    original = reparse_archive.with_name('download.zhiling-private.json')
                    if not original.exists():
                        reparse_archive.replace(original)
            finally:
                queue.db.close()


def result_text(report):
    state = report['state']
    label = '上传组件不支持此运行环境，未上传' if report.get('integration_blocked') else LABELS.get(state, '任务状态：' + state)
    if report.get('connection_check_failed'):
        label = '智凌连接不通，本次未重新解析'
    lines = [label, '', '文件名：' + report['filename']]
    if state == 'succeeded':
        full_path = report.get('full_path')
        lines.append('保存位置：' + full_path if full_path and '/' in full_path else '已上传到夸克网盘。')
    else:
        lines.append('目标类目：' + report['category'])
    if report.get('error'):
        lines.extend(['', report['error']])
    if report.get('review_note'):
        lines.extend(['', report['review_note']])
    if report.get('notice'):
        lines.extend(['', report['notice']])
    if state == 'name_conflict':
        lines.extend(['', '请修改上方点评或类目，再点击“改点评后继续”。已有视频会直接复用。'])
    if state == 'needs_review':
        lines.extend(['', '点击“核对夸克结果”只检查原目录，不会上传。查不到文件也不会自动重传。'])
    if report.get('review_candidate'):
        candidate = report['review_candidate']
        lines.extend(['', '待核对文件：' + candidate['full_path'],
                      '请在夸克播放并确认内容对应，再点击“我已核对，标记完成”。'])
    return '\n'.join(lines)


def build_window(config_path=None, *, runner=run_request):
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    config_path = Path(config_path or default_config_path()).resolve()
    root = tk.Tk()
    root.title('收藏助手 · 自动下载并存入夸克')
    root.geometry('780x780')
    root.minsize(660, 700)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frame = ttk.Frame(root, padding=22)
    frame.grid(sticky='nsew')
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(10, weight=1)
    ttk.Label(frame, text='发来视频，带上你的点评', font=('Microsoft YaHei UI', 16, 'bold')).grid(row=0, column=0, sticky='w')
    ttk.Label(frame, text='点击开始后：自动下载 → 按点评命名 → 存入夸克对应类目。',
              wraplength=670).grid(row=1, column=0, sticky='w', pady=(6, 14))
    ttk.Label(frame, text='视频链接或完整分享文字').grid(row=2, column=0, sticky='w')
    source = ScrolledText(frame, height=3, wrap='word', font=('Microsoft YaHei UI', 11), undo=True)
    source.grid(row=3, column=0, sticky='ew', pady=(5, 12))
    ttk.Label(frame, text='点评 #类目，例如：Ai满足人生活的应用 #二创').grid(row=4, column=0, sticky='w')
    note = ScrolledText(frame, height=3, wrap='word', font=('Microsoft YaHei UI', 11), undo=True)
    note.grid(row=5, column=0, sticky='ew', pady=(5, 8))
    destination = tk.StringVar(value='填写后显示文件名和类目。')
    ttk.Label(frame, textvariable=destination, wraplength=670).grid(row=6, column=0, sticky='w')
    ttk.Label(frame, text='视频号解析会使用智凌额度；本程序不充值，不能保证本次免费。请先在服务商账户确认额度。\n'
              '这是本机测试版：处理期间请保持电脑和此窗口开启。', wraplength=670).grid(row=7, column=0, sticky='w', pady=10)
    buttons = ttk.Frame(frame)
    buttons.grid(row=8, column=0, sticky='ew')
    primary_buttons = ttk.Frame(buttons)
    primary_buttons.pack(fill='x')
    recovery_buttons = ttk.Frame(buttons)
    recovery_buttons.pack(fill='x', pady=(6, 0))
    status = tk.StringVar(value='尚未开始。打开窗口不会下载或上传。')
    ttk.Label(frame, textvariable=status, wraplength=670).grid(row=9, column=0, sticky='w', pady=10)
    output = ScrolledText(frame, height=10, wrap='word', font=('Microsoft YaHei UI', 10), state='disabled')
    output.grid(row=10, column=0, sticky='nsew')
    inbox = messages.Queue()
    state = {'busy': False, 'last': None, 'inputs': None}
    timer = {'id': None, 'restore': None}
    saved_form = config_path.parent / '.runtime' / 'desktop-form.json'

    def show(text):
        output.configure(state='normal')
        output.delete('1.0', 'end')
        output.insert('1.0', text)
        output.configure(state='disabled')

    def values():
        return source.get('1.0', 'end-1c'), note.get('1.0', 'end-1c')

    def refresh_actions():
        for button in (retry_button, reparse_button, revision_button, review_button, confirm_button):
            button.configure(state='disabled')
        if state['busy']: return
        start_button.configure(state='normal')
        last = state['last']
        if not last: return
        try:
            plan = plan_request(load_config(config_path), *values())
        except ValueError:
            return
        if plan['url'] != last.get('source'): return
        if last['state'] == 'name_conflict':
            start_button.configure(state='disabled')
            if plan['filename'] != last['filename'] or plan['category'] != last['category']:
                revision_button.configure(state='normal')
        elif last['state'] == 'needs_review':
            start_button.configure(state='disabled')
            if values() == state['inputs']:
                review_button.configure(state='normal')
                if last.get('review_confirmation'):
                    confirm_button.configure(state='normal')
        elif values() == state['inputs']:
            if last['state'] == 'prepare_failed' and last.get('parse_retry_required'):
                reparse_button.configure(state='normal')
            elif last['state'] in RETRYABLE and not last.get('integration_blocked'):
                retry_button.configure(state='normal')

    def changed(event=None):
        if event is not None:
            if not event.widget.edit_modified():
                return
            event.widget.edit_modified(False)
        if state['busy']:
            return
        refresh_actions()
        try:
            plan = plan_request(load_config(config_path), *values())
            suffix = '（文件名已按保存规则调整）' if plan['filename'] != plan['comment'] + '.mp4' else ''
            destination.set(f"预计类目：{plan['category']}　文件名：{plan['filename']}{suffix}")
        except ValueError as exc:
            destination.set(str(exc))

    def start(retry=False, reparse=False, review=False, confirm=False, revise=False):
        if state['busy']:
            return
        try:
            current = values()
            plan = plan_request(load_config(config_path), *current)
            options_extra = {}
            if (retry or reparse or review) and current != state['inputs']:
                raise ValueError('输入已修改，请使用“开始下载并上传”。')
            if revise:
                last = state['last'] or {}
                if last.get('state') != 'name_conflict' or last.get('source') != plan['url']:
                    raise ValueError('请保留原链接，只修改点评或类目。')
                options_extra['revise_from'] = {
                    'job_key': last.get('job_key') or Path(last['report_path']).parent.name,
                    'task_id': last['id']}
            if review:
                options_extra['review'] = True
            if confirm:
                last = state['last'] or {}
                candidate = last.get('review_candidate')
                if not review or not candidate or not last.get('review_confirmation'):
                    raise ValueError('请先点击“核对夸克结果”。')
                if not messagebox.askyesno('确认已核对网盘文件',
                        '请先在夸克播放以下文件并确认内容与原视频对应：\n'
                        + candidate['full_path'] + '\n\n网盘没有提供足够的文件校验信息。'
                        '\n只有你已确认内容正确，才标记完成；本操作不会重新上传。\n\n是否确认？', parent=root):
                    return
                options_extra['confirmation'] = last['review_confirmation']
            if reparse and not messagebox.askyesno('确认重新尝试一次',
                    '上次没有收到智凌回复，无法确定是否已消耗额度。\n'
                    '重新尝试可能再消耗一次解析额度，程序不会充值。\n\n是否重新尝试一次？', parent=root):
                return
            write_json(saved_form, {'source': current[0], 'note': current[1]})
        except (ValueError, OSError) as exc:
            status.set(str(exc) if isinstance(exc, ValueError) else '无法保存任务输入，请检查本机磁盘。')
            return
        state.update(busy=True, pending_inputs=current)
        source.configure(state='disabled')
        note.configure(state='disabled')
        start_button.configure(state='disabled')
        retry_button.configure(state='disabled')
        reparse_button.configure(state='disabled')
        revision_button.configure(state='disabled')
        review_button.configure(state='disabled')
        confirm_button.configure(state='disabled')
        show('正在处理本次提交，请等待结果。不会自动处理其他任务。')
        status.set('开始处理…')

        def work():
            try:
                options = {'retry': retry, 'progress': lambda message: inbox.put(('progress', message))}
                options.update(options_extra)
                if reparse: options['reparse'] = True
                report = runner(config_path, *current, **options)
                inbox.put(('done', report))
            except (ValueError, RuntimeError) as exc:
                inbox.put(('error', str(exc)))
            except Exception:
                inbox.put(('error', '任务执行异常，输入已保留。请保留本机任务记录，先核对状态，勿反复新建相同任务。'))

        threading.Thread(target=work, daemon=False).start()

    def poll():
        try:
            while True:
                kind, payload = inbox.get_nowait()
                if kind == 'progress':
                    status.set(payload)
                    continue
                state['busy'] = False
                source.configure(state='normal')
                note.configure(state='normal')
                start_button.configure(state='normal')
                if kind == 'done':
                    state['last'] = payload
                    state['inputs'] = state.pop('pending_inputs')
                    status.set('智凌连接不通，本次未重新解析' if payload.get('connection_check_failed') else
                               '上传组件不支持此运行环境，未上传' if payload.get('integration_blocked') else LABELS.get(payload['state'], payload['state']))
                    show(result_text(payload))
                else:
                    state.pop('pending_inputs', None)
                    status.set('未能完成，请查看提示。')
                    show(payload)
                refresh_actions()
        except messages.Empty:
            pass
        timer['id'] = root.after(100, poll)

    def close():
        if state['busy']:
            messagebox.showinfo('正在处理', '请等待本次处理结束后再关闭。可以最小化窗口；不要关闭电脑。', parent=root)
        else:
            root.destroy()

    start_button = ttk.Button(primary_buttons, text='开始下载并上传', command=start)
    start_button.pack(side='left', ipadx=16, ipady=6)
    retry_button = ttk.Button(primary_buttons, text='继续此任务', command=lambda: start(True), state='disabled')
    retry_button.pack(side='left', padx=12, ipady=6)
    reparse_button = ttk.Button(primary_buttons, text='重新尝试解析', command=lambda: start(reparse=True), state='disabled')
    reparse_button.pack(side='left', padx=(0, 12), ipady=6)
    def login_quark():
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable),
                          str(ROOT / 'quark_login.py'), '--config', str(config_path)], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    ttk.Button(primary_buttons, text='登录夸克', command=login_quark).pack(side='left', ipady=6)
    def settings():
        if state['busy']:
            messagebox.showinfo('正在处理', '请等本次任务结束后再修改设置。', parent=root)
            return
        from desktop_setup import show_settings
        show_settings(root, config_path)
        refresh_actions()
    ttk.Button(primary_buttons, text='首次设置', command=settings).pack(side='left', padx=8, ipady=6)
    revision_button = ttk.Button(recovery_buttons, text='改点评后继续', command=lambda: start(revise=True), state='disabled')
    revision_button.pack(side='left', ipady=5)
    review_button = ttk.Button(recovery_buttons, text='核对夸克结果', command=lambda: start(review=True), state='disabled')
    review_button.pack(side='left', padx=12, ipady=5)
    confirm_button = ttk.Button(recovery_buttons, text='我已核对，标记完成', command=lambda: start(review=True, confirm=True), state='disabled')
    confirm_button.pack(side='left', ipady=5)
    source.bind('<<Modified>>', changed)
    note.bind('<<Modified>>', changed)
    try:
        saved = json.loads(saved_form.read_text(encoding='utf-8'))
        if isinstance(saved.get('source'), str) and isinstance(saved.get('note'), str):
            source.insert('1.0', saved['source'][:10000])
            note.insert('1.0', saved['note'][:4000])
    except (OSError, ValueError, AttributeError):
        pass
    root.protocol('WM_DELETE_WINDOW', close)
    def destroyed(event):
        if event.widget is root:
            for name, ident in timer.items():
                if ident is not None:
                    root.after_cancel(ident)
                    timer[name] = None
    root.bind('<Destroy>', destroyed, add='+')
    root.desktop_controls = {'source': source, 'note': note, 'start': start_button,
                             'retry': retry_button, 'reparse': reparse_button,
                             'revise': revision_button, 'review': review_button, 'confirm': confirm_button,
                             'status': status, 'output': output, 'state': state}
    def restore_result():
        if state['busy']: return
        try:
            current = values()
            plan = plan_request(load_config(config_path), *current)
            job = resolve_job(config_path.parent / '.runtime' / 'desktop-jobs', plan, job_key(plan))
            old = json.loads((job / 'desktop-result.json').read_text(encoding='utf-8'))
            if old.get('source') != plan['url'] or old.get('filename') != plan['filename']:
                return
            recovery = parser_recovery(job / 'files' / old['id'], plan['url'])
            old.update(recovery)
            state.update(inputs=current, last=old)
            status.set('智凌连接不通，本次未重新解析' if old.get('connection_check_failed') else
                       LABELS.get(old['state'], old['state']))
            show(result_text(old))
            refresh_actions()
        except (OSError, ValueError, KeyError): pass
    timer['restore'] = root.after(200, restore_result)
    timer['id'] = root.after(100, poll)
    changed()
    return root


if __name__ == '__main__':
    build_window().mainloop()
