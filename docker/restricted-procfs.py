"""Apply Fleet's single restricted-procfs change to checksum-pinned bubblewrap."""

import sys
from pathlib import Path

source = Path(sys.argv[1])
text = source.read_text()
old = 'mount ("proc", dest_path, "proc", MS_NOSUID | MS_NOEXEC | MS_NODEV, NULL)'
new = old.removesuffix("NULL)") + '"subset=pid")'
if text.count(old) != 1:
    raise SystemExit("Unexpected bubblewrap source: restricted procfs patch not applied")
source.write_text(text.replace(old, new))
