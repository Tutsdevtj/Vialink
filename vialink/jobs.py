import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from contextlib import suppress
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

from .policy import MAX_ANALYSIS_DISK, MAX_JOB_DISK, MIB, UserError

LOG = logging.getLogger('vialink.jobs')
ROOT = Path(__file__).resolve().parent.parent
LIMITS = {'analysis': (3, 32, 45, 20 * 60), 'download': (2, 6, 10 * 60, 15 * 60)}


@dataclass
class Job:
    id: str
    folder: Path
    kind: str
    payload: dict
    state: str = 'running'
    expires: float = float('inf')
    result: dict = field(default_factory=dict)
    readers: int = 0


def windows_job(process):
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, f'ViaLink-{uuid4().hex}')
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        limits['JobMemoryLimit'] = 768 * MIB
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        handle = win32api.OpenProcess(access, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
    except Exception:
        job.Close()
        raise
    return job


def folder_size(folder):
    total = 0
    for file in folder.rglob('*'):
        with suppress(FileNotFoundError):
            total += file.stat().st_size
    return total


def supervise(job, stopping):
    folder = job.folder
    (folder / 'input.json').write_text(json.dumps(job.payload), encoding='utf-8')
    deadline = monotonic() + LIMITS[job.kind][2]
    disk_limit = MAX_ANALYSIS_DISK if job.kind == 'analysis' else MAX_JOB_DISK
    windows = os.name == 'nt'
    with (folder / 'worker.log').open('wb') as log:
        process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'vialink.worker', str(folder)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=not windows,
            creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
        )
        group = None
        try:
            if windows:
                group = windows_job(process)
            process.stdin.write(b'1')
            process.stdin.close()
            while process.poll() is None:
                if stopping.wait(.20):
                    raise UserError('A aplicação está encerrando. Tente novamente depois.', 503)
                if monotonic() >= deadline:
                    raise UserError('O tempo limite foi atingido. Tente novamente ou escolha um arquivo menor.', 504)
                if folder_size(folder) > disk_limit:
                    raise UserError('A tarefa ultrapassou o limite de armazenamento temporário.')
            if process.returncode != 0:
                raise RuntimeError(f'Processo de mídia terminou com código {process.returncode}')
        finally:
            if group is not None:
                group.Close()
            elif not windows:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            with suppress(OSError):
                process.stdin.close()
            process.wait()

    return json.loads((folder / 'result.json').read_text(encoding='utf-8'))


def worker_log(folder):
    try:
        with (folder / 'worker.log').open('rb') as log:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 8192))
            return log.read().decode('utf-8', errors='replace').strip()
    except OSError:
        return ''


def delete_folder(folder):
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        pass
    except OSError:
        LOG.warning('Não foi possível limpar %s. A limpeza será repetida.', folder.name, exc_info=True)
        return False
    return True


class Jobs:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vialink-')
        self.items = {}
        self.tasks = set()
        self.stopping = Event()

    def start(self, kind, payload):
        matching = [job for job in self.items.values() if job.kind == kind and job.expires > monotonic()]
        for job in matching:
            if job.payload == payload and job.state in {'running', 'ready'}:
                return job
        active, retained, _, _ = LIMITS[kind]
        if sum(job.state == 'running' for job in matching) >= active:
            raise UserError('Há tarefas em andamento. Aguarde um pouco e tente novamente.', 429)
        stored = [job for job in matching if job.state == 'ready']
        running = [job for job in matching if job.state == 'running']
        if len(stored) + len(running) >= retained:
            raise UserError('O armazenamento temporário está cheio. Aguarde a expiração dos arquivos.', 429)
        # Bound retained failure messages as well as files.
        if len(self.items) >= 100:
            raise UserError('Muitas tarefas recentes. Aguarde um pouco e tente novamente.', 429)

        identifier = uuid4().hex
        folder = Path(self.temp.name) / identifier
        folder.mkdir()
        job = Job(identifier, folder, kind, payload)
        self.items[job.id] = job
        task = asyncio.create_task(self.run(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    def find(self, identifier, kind):
        job = self.items.get(identifier)
        if not job or job.kind != kind or job.expires <= monotonic():
            raise UserError('Esta tarefa expirou. Faça a consulta novamente.', 404)
        return job

    async def run(self, job):
        try:
            result = await asyncio.to_thread(supervise, job, self.stopping)
        except UserError as error:
            result = {'error': str(error), 'status': error.status}
        except Exception:
            LOG.exception('Falha no processo da tarefa %s', job.id)
            result = {'error': 'Houve uma falha ao preparar a tarefa. Tente novamente.', 'status': 500}

        if 'error' in result:
            details = await asyncio.to_thread(worker_log, job.folder)
            LOG.warning('Tarefa %s (%s) falhou: %s\n%s', job.id, job.kind, result['error'], details)
            await asyncio.to_thread(delete_folder, job.folder)
            job.result = result
            job.expires = monotonic() + 180
            job.state = 'error'
            return

        for name in ('input.json', 'result.json', 'progress.json', 'progress.tmp', 'worker.log'):
            try:
                (job.folder / name).unlink(missing_ok=True)
            except OSError:
                LOG.warning('Arquivo temporário ainda em uso na tarefa %s', job.id)
        job.result = result
        job.expires = monotonic() + LIMITS[job.kind][3]
        job.state = 'ready'

    async def reap(self):
        while True:
            await asyncio.sleep(15)
            for identifier, job in list(self.items.items()):
                if job.expires <= monotonic() and not job.readers:
                    if await asyncio.to_thread(delete_folder, job.folder):
                        self.items.pop(identifier, None)

    async def close(self):
        self.stopping.set()
        await asyncio.gather(*self.tasks)
        await asyncio.to_thread(self.temp.cleanup)
