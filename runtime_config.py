"""Shared runtime configuration; loading never activates services or rewrites files."""
import json
from pathlib import Path


def load_runtime_config(path):
    path = Path(path).resolve()
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict) or not isinstance(config.get('categories'), dict):
            raise ValueError()
        runtime = path.parent / '.runtime'
        # Retain the desktop's optional machine-local executable hints. These
        # never contain credentials and never enable the Zhiling adapter.
        try:
            hints = json.loads((runtime / 'desktop-tools.json').read_text(encoding='utf-8'))
            if isinstance(hints, dict):
                for name in ('node', 'ffmpeg', 'ffprobe'):
                    value = hints.get(name)
                    section = config if name == 'node' else config.setdefault('zhiling', {})
                    if (isinstance(section, dict) and section.get(name, name) == name
                            and isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file()):
                        section[name] = value
        except (OSError, ValueError, TypeError):
            pass
        settings = runtime / 'quark-web-settings.json'
        # Presence is intentional, even when the explicit value is malformed.
        # Never silently replace it or fall back to another upload backend.
        if 'quark_web' not in config and settings.is_file():
            config['quark_web'] = json.loads(settings.read_text(encoding='utf-8'))
        return config
    except (OSError, ValueError, TypeError):
        raise ValueError('无法读取有效的收藏助手配置；请保留原文件并检查配置。') from None
