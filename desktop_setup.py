"""Local-only first-run settings. Saving never logs in or starts a download."""
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def save_settings(config_path, mapping, *, zhiling_key='', zhiling_enabled=False, dtk_url='', dtk_key=''):
    config_path = Path(config_path)
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    paths = {}
    for line in mapping.splitlines():
        if not line.strip():
            continue
        if '=' not in line:
            raise ValueError('每行请填写：类目=网盘文件夹/子文件夹')
        name, value = (part.strip() for part in line.split('=', 1))
        parts = value.strip('/').split('/')
        if not name or any(c in name for c in '#/\\\r\n') or name in paths or not all(p.strip() and p not in {'.', '..'} and '\\' not in p for p in parts):
            raise ValueError('类目不能重复，文件夹名称不能为空；多级文件夹用 / 分隔。')
        paths[name] = [p.strip() for p in parts]
    if not paths:
        raise ValueError('请至少填写一组类目和网盘文件夹。')
    for key in (zhiling_key, dtk_key):
        if key and (len(key) > 4096 or any(c.isspace() or ord(c) < 32 for c in key)):
            raise ValueError('密钥不能包含空白字符。')
    dtk_url = dtk_url.strip().rstrip('/')
    if dtk_url:
        u = urlsplit(dtk_url)
        if not u.hostname or u.username or u.password or u.query or u.fragment or (u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in {'localhost','127.0.0.1','::1'})):
            raise ValueError('抖音服务填写 HTTPS 地址，或本机 http://127.0.0.1 地址。')
        if u.hostname == 'demo.douyin.wtf':
            raise ValueError('请使用自己的下载服务，不能填写公共演示站。')
    runtime = config_path.parent / '.runtime'
    zpath = runtime / 'zhiling-api-key.txt'
    dpath = runtime / 'dtk-api-key.txt'
    if zhiling_enabled and not (zhiling_key or zpath.is_file()):
        raise ValueError('启用视频号解析前，请填写自己的智凌 API Key。')
    if dtk_url and not (dtk_key or dpath.is_file() or os.environ.get('DTK_API_KEY')):
        raise ValueError('请填写此抖音下载服务的 API Key。')
    runtime.mkdir(parents=True, exist_ok=True)
    def write_private(path, text):
        temp = path.with_name(path.name + '.tmp')
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
            stream.write(text)
        temp.replace(path)
    if zhiling_key:
        write_private(zpath, zhiling_key)
    if dtk_key:
        write_private(dpath, dtk_key)
        os.environ['DTK_API_KEY'] = dtk_key
    config['categories'] = {name: config.get('categories', {}).get(name, name) for name in paths}
    config['quark_web'] = {**config.get('quark_web', {}), 'session_file': str(runtime / 'quark-web-session.json'), 'category_paths': paths}
    config['zhiling'].update(enabled=bool(zhiling_enabled), api_key_file=str(zpath), api_key_env='ZHILING_API_KEY')
    config['dtk'].update(base_url=dtk_url, api_key_env='DTK_API_KEY')
    write_private(config_path, json.dumps(config, ensure_ascii=False, indent=2))
    return config


def show_settings(parent, config_path):
    import tkinter as tk
    from tkinter import ttk, messagebox
    config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    window = tk.Toplevel(parent)
    window.title('收藏助手 · 首次设置')
    window.geometry('690x650')
    window.transient(parent)
    window.grab_set()
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='先设置自己的网盘和解析服务', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='请先在夸克网盘创建对应文件夹。每行填写“类目=文件夹/子文件夹”。\n例如：二创=视频素材/二创。发布点评时使用 #二创。', wraplength=640).pack(anchor='w', pady=10)
    mapping = tk.Text(frame, height=5, font=('Microsoft YaHei UI', 10))
    mapping.pack(fill='x')
    mapping.insert('1.0', '\n'.join(name+'='+'/'.join(parts) for name,parts in config.get('quark_web',{}).get('category_paths',{}).items()))
    enabled = tk.BooleanVar(value=config.get('zhiling',{}).get('enabled',False))
    ttk.Checkbutton(frame, text='启用视频号解析（需要自己的智凌服务额度）', variable=enabled).pack(anchor='w', pady=(15,4))
    ttk.Label(frame, text='智凌 API Key（已保存时留空即可）').pack(anchor='w')
    zkey = ttk.Entry(frame, show='*'); zkey.pack(fill='x', pady=4)
    ttk.Label(frame, text='抖音 DTK 下载服务地址（不使用抖音可留空）').pack(anchor='w', pady=(12,0))
    durl = ttk.Entry(frame); durl.pack(fill='x', pady=4); durl.insert(0,config.get('dtk',{}).get('base_url',''))
    ttk.Label(frame, text='该下载服务 API Key（已保存时留空即可）').pack(anchor='w')
    dkey = ttk.Entry(frame, show='*'); dkey.pack(fill='x', pady=4)
    ttk.Label(frame, text='解析服务需自行开通，安装包不附赠额度。保存仅更新本机设置；\n回到主窗口后，点击“登录夸克”自行扫码，再提交视频。',wraplength=640).pack(anchor='w',pady=12)
    ttk.Label(frame, text='本机数据位置：'+str(Path(config_path).parent),wraplength=640).pack(anchor='w',pady=4)
    def save():
        try:
            save_settings(config_path,mapping.get('1.0','end'),zhiling_key=zkey.get().strip(),zhiling_enabled=enabled.get(),dtk_url=durl.get(),dtk_key=dkey.get().strip())
        except (OSError,ValueError) as exc:
            messagebox.showerror('设置未保存',str(exc),parent=window);return
        messagebox.showinfo('已保存','设置已保存到本机，尚未登录、下载或上传。',parent=window)
        window.destroy()
    ttk.Button(frame,text='保存设置',command=save).pack(side='right',pady=8)
    ttk.Button(frame,text='稍后设置',command=window.destroy).pack(side='right',padx=12,pady=8)
    parent.wait_window(window)
