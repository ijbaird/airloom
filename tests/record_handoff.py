"""Race-free record handoff between tests and the processes they launch.

Child programs write records with ``write_record`` (pasted into them as
``WRITE_RECORD_SOURCE``): a temporary file in the same directory, then
``os.replace``, so a record is either absent or complete. Tests read records
with ``wait_for_record``, which parses inside its bounded wait, so a record
that is not there yet, or not parseable yet, is retried rather than misread.
"""

import inspect
import time
from pathlib import Path

RECORD_TIMEOUT_S = 20



def write_record(path, text):
    import os
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


# Pasted verbatim into child programs, so they and the tests share one writer.
WRITE_RECORD_SOURCE = "\n" + inspect.getsource(write_record) + "\n"


def nonempty(text: str) -> str:
    if not text:
        raise ValueError("empty record")
    return text


def wait_for_record(path, parse, *, what: str, timeout_s: float = RECORD_TIMEOUT_S, writer=None):
    """Return ``parse(contents)`` once the record at ``path`` parses.

    ``writer`` is an optional Popen: once it has exited, one last read
    decides, since nothing will write the record any more.
    """
    deadline = time.monotonic() + timeout_s
    last_error = None
    while True:
        writer_done = writer is not None and writer.poll() is not None
        try:
            return parse(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError) as exc:
            last_error = exc
        if writer_done:
            raise AssertionError(f"{what}: the writer exited without a complete record at {path} "
                                 f"(last error: {last_error!r})")
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what}: no complete record at {path} after {timeout_s}s "
                                 f"(last error: {last_error!r})")
        time.sleep(0.05)
