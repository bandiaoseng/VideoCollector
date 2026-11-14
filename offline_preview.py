"""Local input preview only. No queue, subprocess, network, or credential use."""
import json
from pathlib import Path

from collector import video_name
from note_input import parse_note
from share_input import parse_share

ROOT = Path(__file__).resolve().parent


def preview(text, note, categories):
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError('分享内容最多 10000 个字符')
    if not isinstance(note, str) or len(note) > 4000:
        raise ValueError('点评最多 4000 个字符')
    source = parse_share(text)
    comment, category = parse_note(note, categories)
    filename = video_name(comment, '.mp4')
    return {**source, 'comment': comment, 'category': category, 'filename': filename,
            'filename_adjusted': filename != comment + '.mp4',
            'state': 'input_checked', 'downloaded': False, 'uploaded': False}


def categories_from_file(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    categories = config.get('categories')
    if not isinstance(categories, dict) or not categories or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in categories.items()
    ):
        raise ValueError('没有已绑定的类目；请保留配置，交由开发者核对。')
    return categories


def build_window(config_path=ROOT / 'config.json'):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title('收藏助手 · 离线输入检查')
    root.geometry('720x650')
    root.minsize(600, 600)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frame = ttk.Frame(root, padding=24)
    frame.grid(sticky='nsew')
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(9, weight=1)
    ttk.Label(frame, text='检查视频链接和点评', font=('Microsoft YaHei UI', 16, 'bold')).grid(
        row=0, column=0, sticky='w')
    ttk.Label(frame, text='离线检查：不会下载、上传或调用智凌，也不消耗解析额度。',
              wraplength=550).grid(row=1, column=0, sticky='w', pady=(8, 20))
    ttk.Label(frame, text='1. 视频分享链接（也可粘贴整段分享文字）').grid(row=2, column=0, sticky='w')
    source = tk.Text(frame, height=4, wrap='word', font=('Microsoft YaHei UI', 11), undo=True)
    source.grid(row=3, column=0, sticky='ew', pady=(6, 12))
    ttk.Label(frame, text='2. 点评与类目，例如：这个开头值得借鉴 #二创').grid(row=4, column=0, sticky='w')
    note = tk.Text(frame, height=3, wrap='word', font=('Microsoft YaHei UI', 11), undo=True)
    note.grid(row=5, column=0, sticky='ew', pady=(6, 8))
    try:
        categories = categories_from_file(config_path)
        category_message = '已绑定类目：' + '、'.join(categories)
        config_error = ''
    except (OSError, ValueError, TypeError, AttributeError):
        categories = {}
        category_message = '类目配置不可用。这个窗口不会修改配置。'
        config_error = '无法读取有效的类目配置，请交由开发者核对 config.json。'
    ttk.Label(frame, text=category_message, wraplength=550).grid(row=6, column=0, sticky='w')
    result = tk.StringVar(value='填写上面两项，再点击“检查输入”。')

    def check():
        try:
            if config_error:
                raise ValueError(config_error)
            plan = preview(source.get('1.0', 'end-1c'), note.get('1.0', 'end-1c'), categories)
        except ValueError as exc:
            result.set('输入还需修改：' + str(exc) + '\n\n尚未下载、尚未上传。')
            return
        platform = '视频号' if plan['platform'] == 'wechat' else '抖音'
        adjusted = '\n文件名包含不适合保存的字符，已按正式程序的规则调整。' if plan['filename_adjusted'] else ''
        result.set(f"输入检查通过（不是归档成功）\n\n平台：{platform}\n"
                   f"目标类目：{plan['category']}\n预计文件名：{plan['filename']}{adjusted}\n\n"
                   '尚未验证链接是否有效、视频能否下载或网盘能否上传。\n尚未下载、尚未上传。')

    button = ttk.Button(frame, text='检查输入', command=check)
    button.grid(row=7, column=0, sticky='ew', pady=16, ipady=6)
    ttk.Separator(frame).grid(row=8, column=0, sticky='ew')
    output = ttk.Label(frame, textvariable=result, wraplength=550, anchor='nw', justify='left')
    output.grid(row=9, column=0, sticky='nsew', pady=12)

    def invalidate(event):
        widget = event.widget
        if widget.edit_modified():
            result.set('内容已修改，请重新检查。尚未下载、尚未上传。')
            widget.edit_modified(False)

    source.bind('<<Modified>>', invalidate)
    note.bind('<<Modified>>', invalidate)
    # Named handles for local widget verification; they never start a worker.
    root.preview_controls = (source, note, button, result)
    source.focus_set()
    return root


if __name__ == '__main__':
    build_window().mainloop()
