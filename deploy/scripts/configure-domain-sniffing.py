from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


SNIFFER_BLOCK = """sniffer:
  enable: true
  force-dns-mapping: true
  parse-pure-ip: true
  override-destination: false
  sniff:
    HTTP:
      ports: [80, 8080-8880]
      override-destination: true
    TLS:
      ports: [443, 8443]
    QUIC:
      ports: [443, 8443]
"""


def enable_domain_sniffing(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    if re.search(r"(?m)^sniffer:\s*(?:#.*)?$", source):
        raise FileExistsError("sniffer is already configured")
    destination = source.rstrip("\n") + "\n\n" + SNIFFER_BLOCK
    metadata = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config.sniffer.next.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(destination)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_name, stat.S_IMODE(metadata.st_mode))
        if os.name != "nt":
            os.chown(temporary_name, metadata.st_uid, metadata.st_gid)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Append the safe Mihomo pure-IP domain-sniffer block.")
    parser.add_argument("config", type=Path)
    arguments = parser.parse_args()
    try:
        enable_domain_sniffing(arguments.config)
    except FileExistsError as error:
        print(str(error), file=sys.stderr)
        return 3
    except OSError as error:
        print(f"could not configure sniffer: {error}", file=sys.stderr)
        return 1
    print("DOMAIN_SNIFFER=enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
