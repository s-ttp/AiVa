"""supervisord event listener: stop the container when a program turns FATAL (it kept crashing).

A gateway without Postgres, Redis or OPA fails closed but cannot serve, so a clean restart of the whole
Space is the better outcome. Speaks the listener protocol directly (stdlib only).
"""

import os
import signal
import sys

# Programs the gateway serves without: the database backup (db_backup.py). Their failure is logged only.
OPTIONAL = frozenset({"backup"})


def main() -> None:
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        header = dict(token.split(":", 1) for token in sys.stdin.readline().split())
        payload = sys.stdin.read(int(header.get("len", "0")))
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()
        process = dict(t.split(":", 1) for t in payload.split() if ":" in t).get("processname")
        if header.get("eventname") == "PROCESS_STATE_FATAL" and process in OPTIONAL:
            sys.stderr.write(f"fatal-listener: {process} is optional; the container keeps running\n")
            sys.stderr.flush()
        elif header.get("eventname") == "PROCESS_STATE_FATAL":
            sys.stderr.write(f"fatal-listener: {payload.strip()}; stopping the container\n")
            sys.stderr.flush()
            os.kill(os.getppid(), signal.SIGTERM)


if __name__ == "__main__":
    main()
