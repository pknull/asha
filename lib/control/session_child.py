"""Linux process-group guardian; the harness starts only after durable custody."""
import ctypes
import os
import signal
import subprocess
import sys


def main():
    release_fd, parent = int(sys.argv[1]), int(sys.argv[2])
    if os.getpgrp() != os.getpid():
        return 125

    def end_group(_signal, _frame):
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, end_group)
    signal.signal(signal.SIGINT, end_group)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return 125
    if os.getppid() != parent:
        return 125
    try:
        if os.read(release_fd, 1) != b"1":
            return 125
    finally:
        os.close(release_fd)
    process = subprocess.Popen(sys.argv[3:], close_fds=True)
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
