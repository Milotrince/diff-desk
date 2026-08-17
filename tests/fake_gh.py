"""Stand in for the gh command, answering exactly as the test in charge asked it to.

The reply is read at each call from the file named by FAKE_GH_SCRIPT, so one running desk can be made to succeed, to be
refused, or to be unreachable, without a network or a login.
"""

import json
import os
import pathlib
import sys

asked = json.loads(pathlib.Path(os.environ["FAKE_GH_SCRIPT"]).read_text())
sys.stdin.read()
sys.stdout.write(asked.get("out", ""))
sys.stderr.write(asked.get("err", ""))
raise SystemExit(asked.get("code", 0))
