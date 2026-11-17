import os, sys, json, traceback
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

def data_root(name, variable):
    value = os.environ.get(variable)
    path = Path(value) if value else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / name
    if not path.is_absolute():
        raise ValueError("数据目录必须是绝对路径")
    path.mkdir(parents=True, exist_ok=True)
    temp = path / "temp"
    temp.mkdir(exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(temp)
    import tempfile
    tempfile.tempdir = str(temp)
    return path

def main():
    data = data_root("VideoCollector", "VIDEOCOLLECTOR_DATA_DIR")
    os.environ["VIDEOCOLLECTOR_DATA_DIR"] = str(data)
    config_path = data / "config.json"
    if not config_path.exists():
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config["data_dir"] = str(data / "data")
        config["quark_web"]["session_file"] = str(data / ".runtime" / "quark-web-session.json")
        for tool in ("ffmpeg", "ffprobe"):
            config["zhiling"][tool] = str(ROOT.parent / "tools" / (tool + ".exe"))
        with config_path.open("x", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
    os.chdir(data)
    # DTK's adapter reads its key from the process environment; never global.
    dtk_key = data / ".runtime" / "dtk-api-key.txt"
    if dtk_key.is_file():
        os.environ["DTK_API_KEY"] = dtk_key.read_text(encoding="utf-8").strip()
    from desktop_app import build_window
    if "--self-test" in sys.argv:
        import subprocess
        report = Path(sys.argv[sys.argv.index("--self-test") + 1])
        result = {"ok": False, "data": str(data), "python": sys.prefix}
        try:
            import httpx, qrcode
            from PIL import ImageTk
            config = json.loads(config_path.read_text(encoding="utf-8"))
            assert not config["categories"], "Self-test requires a new profile"
            for tool in ("ffmpeg", "ffprobe"):
                subprocess.run([config["zhiling"][tool], "-version"], check=True, capture_output=True, timeout=10)
            window = build_window(config_path)
            window.after(800, window.destroy)
            window.mainloop()
            result.update(ok=True, ui=True, mediaTools=True, emptyProfile=True)
        except Exception:
            result["error"] = traceback.format_exc()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if result["ok"] else 1
    window = build_window(config_path)
    if not json.loads(config_path.read_text(encoding="utf-8")).get("categories"):
        from desktop_setup import show_settings
        window.after(100, lambda: show_settings(window, config_path))
    window.mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
