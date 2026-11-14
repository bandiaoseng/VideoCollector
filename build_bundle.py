"""Build a source-only deployment archive using an explicit allowlist."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
DESTINATION_FILES = ['destination_check.py', 'test_destination_check.py', 'test_revisions.py']
FILES = ['collector.py', 'upload_evidence.py', 'test_upload_evidence.py', 'process_runner.py', 'test_process_runner.py', 'share_input.py', 'note_input.py', 'web_store.py', 'web_app.py',
         'worker.py', 'douyin_f2.py', 'dtk_download.py', 'zhiling_download.py',
         'configure_zhiling.py', 'configure-zhiling.cmd', 'test_zhiling_download.py', 'service_check.py', 'requirements.txt',
         'requirements.lock.txt', 'requirements-dev.txt', 'requirements-downloader.txt', 'constraints-downloader.txt',
         'config.example.json', 'README.md', 'ZHILING_SETUP.md', 'build_bundle.py',
         'offline_preview.py', 'preview-input.cmd', 'test_offline_preview.py', 'release-status.json',
         'desktop_app.py', 'start-collector.cmd', 'test_desktop_app.py',
         'parse_recovery.py', 'desktop_recovery.py', 'test_desktop_recovery.py', 'test_quark_recovery.py',
         'runtime_config.py', 'uploader_factory.py', 'test_runtime_integration.py', 'test_web_runtime.py',
         'quark_login_cli.py', 'test_quark_login_cli.py',
         'web_recovery.py', 'test_web_recovery.py',
         'test_web_reparse.py',
         'execution_coordination.py', 'test_execution_coordination.py',
         'test_session_coordination.py',
         'storage_guard.py', 'test_storage_guard.py',
         'quark_web.py', 'quark_web_uploader.py', 'quark_login.py', 'test_quark_web.py', 'requirements-desktop.txt',
         'test_collector.py', 'test_cancellation.py', 'test_web.py', 'test_preview.mjs', 'test_submission.mjs', 'test_draft_editor.mjs', 'test_dtk_download.py', 'test_douyin_f2.py', 'test_service_check.py',
         'validation/wechat_worker_offline.mjs']


def main():
    paths = [ROOT / name for name in FILES + DESTINATION_FILES]
    for directory in ('web', 'deploy'):
        paths.extend(p for p in (ROOT / directory).rglob('*') if p.is_file())
    manifest = {}
    for path in paths:
        if not path.resolve().is_relative_to(ROOT):
            raise ValueError('Bundle source escaped workspace')
        name = path.relative_to(ROOT).as_posix()
        if name == 'config.json' or name.endswith('.env'):
            raise ValueError('Live configuration must not be bundled')
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    output = ROOT / 'releases' / 'video-collector-source.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr('bundle-manifest.json', json.dumps(manifest, indent=2))
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert all(hashlib.sha256(archive.read(name)).hexdigest() == digest for name,digest in manifest.items())
    print(json.dumps({'archive':str(output),'files':len(manifest),'bytes':output.stat().st_size,
                      'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
