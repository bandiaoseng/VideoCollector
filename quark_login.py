"""First-time independent Quark QR sign-in; no upload or media download."""
from pathlib import Path
import queue
import threading
from quark_web import save_private
from quark_login_cli import run_login


def run_desktop_login(config_path, status_file, inbox, stopped, *, login=run_login):
    """Share staged login, session exclusion and cancellation with headless hosts."""
    import qrcode
    def qr_writer(url, path):
        inbox.put(('qr', qrcode.make(url).convert('RGB').resize((360, 360))))
        save_private(status_file, {'state': 'waiting_for_scan'})
    def emit(message):
        if not message.startswith(('二维码已生成：', '二维码已刷新，', '请在受信任的设备')):
            inbox.put(('status', message))
    save_private(status_file, {'state': 'requesting_qr'})
    result = login(config_path, start_login=True, timeout=300,
                   qr_writer=qr_writer, cancelled=stopped.is_set,
                   wait=stopped.wait, emit=emit)
    save_private(status_file, {'state': result})
    return result


def main():
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageTk
    root = tk.Tk()
    root.title('收藏助手 · 登录夸克（自动刷新）')
    root.geometry('490x590')
    ttk.Label(root, text='请用手机夸克扫码，并在手机上确认登录', wraplength=440,
              font=('Microsoft YaHei UI', 13)).pack(padx=20, pady=18)
    image = ttk.Label(root); image.pack()
    status = tk.StringVar(value='正在获取夸克登录二维码…')
    ttk.Label(root, textvariable=status, wraplength=440).pack(padx=20, pady=18)
    ttk.Label(root, text='登录状态只保存在本机。此窗口不会下载或上传视频。', wraplength=440).pack(padx=20)
    inbox = queue.Queue(); stopped = threading.Event()
    status_file = Path(__file__).resolve().parent / '.runtime' / 'quark-login-status.json'

    def work():
        try:
            run_desktop_login(Path(__file__).resolve().parent / 'config.json', status_file, inbox, stopped)
        except Exception:
            save_private(status_file, {'state': 'failed'})
            inbox.put(('status', '登录连接未完成，请关闭后重试；未上传文件。'))

    def poll():
        try:
            while True:
                kind, value = inbox.get_nowait()
                if kind == 'qr':
                    root.qr_image = ImageTk.PhotoImage(value)
                    image.configure(image=root.qr_image)
                    status.set('等待扫码确认，二维码每两分钟自动刷新；本次最多等待五分钟。')
                else: status.set(value)
        except queue.Empty: pass
        if not stopped.is_set(): root.after(200, poll)

    def close():
        stopped.set(); root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    threading.Thread(target=work, daemon=True).start()
    root.after(200, poll)
    root.mainloop()


if __name__ == '__main__':
    main()
