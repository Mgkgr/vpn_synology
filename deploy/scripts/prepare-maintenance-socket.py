"""Prepare the persistent IPC directory, without starting or authorizing jobs."""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.host_service import prepare_socket_parent as prepare


if __name__ == '__main__':
    if os.name != 'posix' or os.geteuid() != 0 or len(sys.argv) != 1:
        raise SystemExit('linux_root_without_arguments_required')
    prepare()
    print('MAINTENANCE_SOCKET_DIRECTORY=ready')
