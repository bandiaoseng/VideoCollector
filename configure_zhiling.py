"""Save a user's key locally; no API calls and no activation of pending tasks."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys


def save_key(config_path, key):
    if not isinstance(key, str) or not key or len(key) > 4096 or any(c.isspace() or ord(c) < 32 for c in key):
        raise ValueError('密钥不能为空，也不能包含空白字符')
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict) or not isinstance(config.get('zhiling', {}), dict):
        raise ValueError('配置格式不正确，未修改')
    if config.get('zhiling', {}).get('enabled') is True:
        raise ValueError('请先停用智凌分支，再更新密钥，避免后台立即使用新授权')
    folder = config_path.parent / '.runtime'
    folder.mkdir(mode=0o700, exist_ok=True)
    key_path = folder / 'zhiling-api-key.txt'
    temporary = key_path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(key)
    temporary.replace(key_path)
    config['zhiling'] = {**config.get('zhiling', {}), 'enabled': False,
                         'api_key_file': str(key_path), 'api_key_env': 'ZHILING_API_KEY'}
    temporary_config = config_path.with_name(config_path.name + '.tmp')
    fd = os.open(temporary_config, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    temporary_config.replace(config_path)
    return key_path


def build_window(config_path):
    """Local form; clipboard is read only when the user presses Paste."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title('智凌 API 密钥配置')
    root.geometry('540x285')
    root.minsize(480, 270)
    root.columnconfigure(0, weight=1)
    frame = ttk.Frame(root, padding=24)
    frame.grid(sticky='nsew')
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text='填写智凌 API Key', font=('Microsoft YaHei UI', 13, 'bold')).grid(
        row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))
    ttk.Label(frame, text='只保存在这台电脑，不充值、不消耗额度、不启动下载。',
              wraplength=450).grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 16))
    value = tk.StringVar()
    status = tk.StringVar(value='请先复制密钥，再点击右侧“粘贴”。')
    entry = ttk.Entry(frame, textvariable=value, show='*', font=('Consolas', 12))
    entry.grid(row=2, column=0, sticky='ew', ipady=5)

    def paste():
        try:
            text = root.clipboard_get().strip()
        except tk.TclError:
            status.set('剪贴板里没有可粘贴的文字，请重新复制密钥。')
            return
        if not text or len(text) > 4096:
            status.set('复制的内容为空或过长，请只复制 API Key。')
            return
        value.set(text)
        entry.focus_set()
        entry.icursor(tk.END)

    def save(event=None):
        try:
            save_key(config_path, value.get().strip())
        except (OSError, ValueError):
            status.set('保存失败：请检查密钥是否完整、无空格，且智凌分支未启用。')
            return
        value.set('')
        status.set('密钥已保存到本机。未启动下载，可以关闭窗口。')

    paste_button = ttk.Button(frame, text='粘贴', command=paste)
    paste_button.grid(row=2, column=1, padx=(10, 0))
    ttk.Label(frame, text='粘贴成功会显示一串 *，不会显示密钥原文。').grid(
        row=3, column=0, columnspan=2, sticky='w', pady=(8, 8))
    ttk.Label(frame, textvariable=status, wraplength=450).grid(
        row=4, column=0, columnspan=2, sticky='w', pady=(0, 16))
    save_button = ttk.Button(frame, text='保存到本机', command=save, state='disabled')
    save_button.grid(row=5, column=0, sticky='e')
    ttk.Button(frame, text='关闭', command=root.destroy).grid(row=5, column=1, padx=(10, 0))

    def changed(*_):
        count = len(value.get().strip())
        status.set(f'已填入 {count} 个字符，点击“保存到本机”。' if count else '尚未填写密钥。')
        save_button.configure(state='normal' if count else 'disabled')

    value.trace_add('write', changed)
    entry.bind('<Return>', save)
    entry.focus_set()
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description='本机保存智凌密钥，不请求接口、不充值、不启动任务')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--gui', action='store_true', help='打开可粘贴的本机密码输入窗口')
    args = parser.parse_args(argv)
    if args.gui:
        root = build_window(args.config)
        root.mainloop()
        return 0
    if not sys.stdin.isatty():
        print('请在自己的终端窗口运行此工具；不要把密钥放进命令行或聊天。')
        return 1
    try:
        print('此操作只保存密钥，视频号分支仍保持关闭，不消耗额度。')
        key = getpass.getpass('请粘贴智凌 API Key（输入不显示），然后回车：').strip()
        save_key(args.config, key)
        print('密钥已保存到本机。未启用接口，也未发起下载或上传。')
        return 0
    except (OSError, ValueError):
        print('保存失败：请确认配置有效、智凌分支未启用，且密钥非空。密钥不会显示。')
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n已取消。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
