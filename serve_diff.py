"""Serve the diff desk locally: rescan any repository, record every comment, and hand them to a session in order.

Endpoints, all on 127.0.0.1 so nothing is exposed off the machine:
  GET  /                      the page
  GET  /data                  the payload the page renders
  GET  /refs?dir=&base=       branches ahead of a base, for the source picker
  POST /scan                  {dir, base, refs} - regenerate the payload and return it
  GET  /comments?since=N      every recorded comment past the cursor, each with its seq and batch
  POST /comments              a comment, or a batch of them, as submitted from the page
  POST /resolve               {seq: [...], answer} - mark comments addressed, which the page then shows
  POST /publish               {repo, pr, summary, comments} - post the batch to a pull request as one review
"""

import json
import os
import pathlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import gen_diff_data

HERE = pathlib.Path(__file__).parent
HOME = gen_diff_data.home()
PAGE = HOME / "diff_desk.html"
TEMPLATE = HERE / "diff_desk_template.html"
DATA = HOME / "diff_data.json"
NOTES = HOME / "comments.jsonl"
PORT = int(os.environ.get("DIFF_DESK_PORT", "8787"))


def read_notes():
    """Every recorded comment, numbered: a row written before the cursor existed is numbered by its position."""
    if not NOTES.exists():
        return []
    rows = [json.loads(line) for line in NOTES.read_text().splitlines() if line.strip()]
    for index, row in enumerate(rows, start=1):
        row.setdefault("seq", index)
        row.setdefault("batch", 0)
        row.setdefault("state", "open")
    return rows


def write_notes(rows):
    NOTES.write_text("".join(json.dumps(row) + "\n" for row in rows))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", kind="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        route = urlparse(self.path)
        query = parse_qs(route.query)
        path = route.path.rstrip("/")
        if path in ("", "/index.html"):
            print(f"PAGE served to {self.headers.get('User-Agent', '?')}", flush=True)
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/data":
            self._send(200, DATA.read_bytes(), "application/json")
        elif path == "/refs":
            root = query.get("dir", ["."])[0]
            base = query.get("base", ["upstream/main"])[0]
            upstream = gen_diff_data.canonical_repo(root)
            pulls = gen_diff_data.pull_requests(root, upstream) if upstream else {}
            self._json(
                {
                    "root": gen_diff_data.run(root, "rev-parse", "--show-toplevel").strip(),
                    "current": gen_diff_data.run(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                    "upstream": upstream,
                    "refs": gen_diff_data.ahead_refs(root, base),
                    "pulls": sorted(pulls.values(), key=lambda row: -row["number"]),
                }
            )
        elif path == "/lines":
            self._lines(query)
        elif path == "/favicon.ico":
            self._send(204)
        elif path == "/comments":
            since = int(query.get("since", ["0"])[0])
            self._json([row for row in read_notes() if row.get("seq", 0) > since])
        else:
            self._send(404)

    def _lines(self, query):
        """A slice of a file at a revision, which is how the page fills the gaps between hunks."""
        root = pathlib.Path(query.get("dir", ["."])[0])
        rev = query.get("rev", [""])[0]
        name = query.get("path", [""])[0]
        text = gen_diff_data.run(root, "show", f"{rev}:{name}") if rev else (root / name).read_text()
        rows = text.split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        low = max(1, int(query.get("from", ["1"])[0]))
        high = min(len(rows), int(query.get("to", [str(len(rows))])[0]))
        self._json({"total": len(rows), "from": low, "to": high, "lines": rows[low - 1 : high]})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/comments":
            self._record()
        elif path == "/scan":
            self._scan()
        elif path == "/resolve":
            self._resolve()
        elif path == "/publish":
            self._publish()
        else:
            self._send(404)

    def _scan(self):
        """Rebuild the payload for the requested repository, base and refs, and rebuild the page around it."""
        order = self._body()
        root = order.get("dir") or "."
        base = order.get("base") or "upstream/main"
        refs = [ref for ref in (order.get("refs") or []) if ref]
        print(f"SCAN {root} {base} {refs or '(every branch ahead)'}", flush=True)
        try:
            payload = gen_diff_data.collect(root, base, refs)
        except Exception as error:  # noqa: BLE001 - whatever went wrong belongs on the page, not in a traceback
            print(f"SCAN FAILED {error}", flush=True)
            self._json({"ok": False, "error": f"{type(error).__name__}: {error}"})
            return
        if not payload["branches"]:
            self._json({"ok": False, "error": f"nothing ahead of {base} in {root}"})
            return
        DATA.write_text(json.dumps(payload, separators=(",", ":")))
        if TEMPLATE.exists():
            PAGE.write_text(gen_diff_data.render_page(TEMPLATE.read_text(), payload))
        files = sum(len(entry["files"]) for entry in payload["branches"])
        print(f"SCANNED {len(payload['branches'])} branch(es), {files} file diffs", flush=True)
        self._json({"ok": True, "data": payload})

    def _record(self):
        body = self._body()
        # A review arrives as a batch; a single comment is the batch of one.
        batch = body if isinstance(body, list) else [body]
        rows = read_notes()
        seq = max((row.get("seq", 0) for row in rows), default=0)
        group = max((row.get("batch", 0) for row in rows), default=0) + 1
        for note in batch:
            seq += 1
            note["seq"] = seq
            note["batch"] = group
            note["state"] = "open"
            rows.append(note)
        write_notes(rows)
        print(f"BATCH {group}: {len(batch)} comment(s) submitted", flush=True)
        for note in batch:
            span = str(note.get("line", "?"))
            if note.get("endLine") and note["endLine"] != note.get("line"):
                span += f"-{note['endLine']}"
            text = " ".join(str(note.get("text", "")).split())
            print(f"  COMMENT [{note['seq']}] {note.get('path', '?')}:{span} :: {text}", flush=True)
        self._json({"ok": True, "batch": group, "seq": seq})

    def _resolve(self):
        """Mark comments addressed, so the page shows what a session has already dealt with."""
        order = self._body()
        wanted = set(order.get("seq") or [])
        answer = order.get("answer", "")
        rows = read_notes()
        touched = 0
        for row in rows:
            if row.get("seq") in wanted:
                row["state"] = "resolved"
                if answer:
                    row["answer"] = answer
                touched += 1
        write_notes(rows)
        print(f"RESOLVED {touched} comment(s)", flush=True)
        self._json({"ok": True, "resolved": touched})

    def _publish(self):
        """Post the batch to the pull request as one review, ranges included."""
        order = self._body()
        review = {"event": "COMMENT", "body": order.get("summary") or "Review from the diff desk.", "comments": []}
        for note in order.get("comments", []):
            side = "LEFT" if note.get("side") == "old" else "RIGHT"
            comment = {
                "path": note["path"],
                "body": note["text"],
                "line": note.get("endLine") or note["line"],
                "side": side,
            }
            if note.get("endLine") and note["endLine"] != note["line"]:
                comment["start_line"] = note["line"]
                comment["start_side"] = side
            review["comments"].append(comment)
        target = f"repos/{order['repo']}/pulls/{order['pr']}/reviews"
        print(f"PUBLISH {len(review['comments'])} comment(s) -> {target}", flush=True)
        done = subprocess.run(
            ["gh", "api", "--method", "POST", target, "--input", "-"],
            input=json.dumps(review),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if done.returncode == 0:
            url = json.loads(done.stdout or "{}").get("html_url", "")
            print(f"PUBLISHED {url}", flush=True)
            self._json({"ok": True, "url": url})
        else:
            error = " ".join((done.stderr or done.stdout).split())[:400]
            print(f"PUBLISH FAILED {error}", flush=True)
            self._json({"ok": False, "error": error})

    def log_message(self, *args):
        pass


def main():
    print(f"diff desk on http://127.0.0.1:{PORT}/  (comments -> {NOTES})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
