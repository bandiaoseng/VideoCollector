"""Long-running worker, independent of browser and web-server request lifetime."""
import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from collector import Queue, WorkerLock, process_one
from runtime_config import load_runtime_config
from execution_coordination import execution_entry, coordination_root


def run(config_path, stop=None, process=process_one):
    stop = stop or threading.Event()
    config_path = Path(config_path).resolve()
    config = load_runtime_config(config_path)
    root = Path(config.get('data_dir', 'data'))
    if not root.is_absolute():
        root = config_path.parent / root
    queue = Queue(root)
    heartbeat = root / 'worker-heartbeat.json'
    pulse_stop = threading.Event()

    def pulse():
        while not pulse_stop.is_set():
            tmp = heartbeat.with_suffix('.tmp')
            tmp.write_text(json.dumps({'updated': time.time()}), encoding='utf-8')
            tmp.replace(heartbeat)
            pulse_stop.wait(10)

    try:
        with execution_entry(config_path, config, 'worker', root), WorkerLock(root):
            queue.recover()
            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                while not stop.is_set():
                    current = load_runtime_config(config_path)
                    if current.get('data_dir', 'data') != config.get('data_dir', 'data'):
                        raise RuntimeError('Restart worker after changing data_dir')
                    if coordination_root(config_path,current).resolve() != coordination_root(config_path,config).resolve():
                        raise RuntimeError('执行协调位置已变化，请先停止后台再修改配置。')
                    task = process(queue, current)
                    if task is None:
                        stop.wait(2)
                    else:
                        print(json.dumps({'id': task['id'], 'state': task['state']}, ensure_ascii=False), flush=True)
                        if task.get('notice'):
                            print(task['notice'], flush=True)
            finally:
                pulse_stop.set()
                thread.join(15)
                heartbeat.unlink(missing_ok=True)
    finally:
        queue.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=os.environ.get('COLLECTOR_CONFIG', Path(__file__).with_name('config.json')))
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    run(args.config, stopped)
