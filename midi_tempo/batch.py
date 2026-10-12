"""Check or fix many files at once, using every processor core.

Each file is independent, so the work spreads across processes (not threads:
Python runs one thread at a time). Results come back in the original order,
progress is reported as files finish, and a stop request ends the batch early.

The workers only import the standard-library engine, never the window code, so
they start quickly, and a frozen app needs multiprocessing.freeze_support()
called first (app.py does).
"""
from __future__ import annotations

import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Callable

from .core import MidiError, analyze, fix_file, read_midi

PARALLEL_FROM = 8  # fewer files than this: starting worker processes costs more than it saves
last_run = {"parallel": False, "fell_back": False}  # what the last batch actually did; the self-test reads it


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)  # leave one core for the window and everything else


def _check_one(job: tuple[str, str]):
    path, name = job
    try:
        return path, analyze(read_midi(path), name), None
    except (MidiError, OSError) as e:
        return path, None, f"{name}: could not check it, {e}."


def _fix_one(job: tuple):
    path, notes, project = (job + (None,))[:3]  # (path, notes) or (path, notes, REAPER project)
    try:
        out, said = fix_file(path, notes=notes, project=project)
        return path, out, said, None
    except PermissionError as e:
        return path, None, None, str(e)
    except (MidiError, OSError) as e:
        return path, None, [f"{os.path.basename(path)}: not fixed, {e}."], None


def _run(func, jobs: list, workers: int | None, progress: Callable[[int, int], None] | None,
         stop: Callable[[], bool] | None) -> tuple[list, bool]:
    """Returns (results in job order, stopped early)."""
    total, results = len(jobs), []
    workers = workers or default_workers()
    last_run.update(parallel=False, fell_back=False)
    if workers <= 1 or total < PARALLEL_FROM:
        for i, job in enumerate(jobs, 1):
            if stop and stop():
                return results, True
            results.append(func(job))
            if progress:
                progress(i, total)
        return results, False
    ctx = mp.get_context("spawn")  # the same on every platform, and safe next to a GUI's threads
    chunk = max(1, min(32, total // (workers * 8)))
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            it = pool.map(func, jobs, chunksize=chunk)
            for i, res in enumerate(it, 1):
                results.append(res)
                if progress:
                    progress(i, total)
                if stop and stop():
                    pool.shutdown(wait=False, cancel_futures=True)
                    last_run["parallel"] = True
                    return results, True
        last_run["parallel"] = True
        return results, False
    except (BrokenProcessPool, OSError, RuntimeError):
        last_run["fell_back"] = True
        # Worker processes could not start or died (a locked-down machine, a frozen app missing
        # freeze_support). Finish the rest on this core rather than fail the whole batch.
        rest, stopped = _run(func, jobs[len(results):], 1,
                             (lambda i, t: progress(len(results) + i, total)) if progress else None, stop)
        return results + rest, stopped


def check_many(jobs: list[tuple[str, str]], workers: int | None = None, progress=None, stop=None):
    """jobs: (path, display name). Each result: (path, Report or None, error or None)."""
    return _run(_check_one, jobs, workers, progress, stop)


def fix_many(jobs: list[tuple], workers: int | None = None, progress=None, stop=None):
    """jobs: (path, fix notes too). Each result: (path, saved path or None, sentences, permission error)."""
    return _run(_fix_one, jobs, workers, progress, stop)
