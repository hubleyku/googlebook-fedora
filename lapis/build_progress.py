"""Terminal progress and a persistent command log for image builds."""
from contextlib import contextmanager
import shlex
import subprocess
import sys
import threading
import time


class BuildProgress:
    def __init__(self, log=None, stream=None, interval=None):
        self.log = log
        self.stream = stream if stream is not None else sys.stdout
        self.terminal = self.stream.isatty()
        self.interval = interval if interval is not None else (2 if self.terminal else 10)
        self.detail = ''
        self.lock = threading.Lock()

    def _print(self, message):
        with self.lock:
            print(message, file=self.stream, flush=True)

    @contextmanager
    def stage(self, number, total, label):
        title = f'[{number}/{total}] {label}'
        self.detail = ''
        started = time.monotonic()
        stop = threading.Event()
        self._print(title + '…')

        def heartbeat():
            while not stop.wait(self.interval):
                detail = f' — {self.detail}' if self.detail else ' — working'
                self._print(f'{title}{detail} ({time.monotonic() - started:.0f}s elapsed)')

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        except BaseException:
            stop.set()
            thread.join()
            self._print(f'{title} — stopped after {time.monotonic() - started:.1f}s')
            raise
        else:
            stop.set()
            thread.join()
            detail = f'; {self.detail}' if self.detail else ''
            self._print(f'{title} — done ({time.monotonic() - started:.1f}s{detail})')

    def bytes(self, label):
        started = time.monotonic()

        def update(done, total):
            elapsed = max(time.monotonic() - started, 0.001)
            self.detail = (f'{label}: {done / total:.0%}, '
                           f'{done / 1024**2:.0f}/{total / 1024**2:.0f} MiB, '
                           f'{done / elapsed / 1024**2:.1f} MiB/s') if total else label
        return update

    def run(self, argv, **kwargs):
        """Keep verbose tool output in the log while the stage heartbeat runs."""
        if self.log is not None:
            print('+ ' + shlex.join(map(str, argv)), file=self.log, flush=True)
            kwargs.setdefault('stdout', self.log)
            kwargs.setdefault('stderr', self.log)
        return subprocess.run(list(map(str, argv)), check=True, **kwargs)
