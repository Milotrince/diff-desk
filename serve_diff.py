"""Serve the diff desk locally: rescan any repository, record every comment, and hand them to a session in order.

Endpoints, all on 127.0.0.1 so nothing is exposed off the machine:
  GET  /                      the page
  GET  /data                  the payload the page renders
  GET  /refs?dir=&base=       branches ahead of a base, for the source picker
  POST /scan                  {dir, base, refs} - regenerate the payload and return it
  GET  /comments?since=N      every recorded comment past the cursor, each with its seq and batch
  POST /comments              {comments: [...], github: bool} - a batch as submitted, or a bare list of comments
  POST /edit                  {seq, text} - rewrite a comment, keeping what it said before
  POST /reply                 {seq, text, who} - add a reply to a comment, from the session or from the reviewer
  POST /resolve               {seq: [...], answer, resolved, who} - close comments, or reopen them
  POST /publish               {repo, pr, summary, seq} - post those comments as one review; everything still owed
                              when seq is omitted, which is how a post that did not land is retried

A comment is a thread: the reviewer's remark plus replies from either side, each stamped with who wrote it. A reply
leaves the thread open; only resolving closes it, either side may do so, and a resolved thread keeps its text and every
reply - closing it hides nothing and deletes nothing. Rewriting a comment keeps every earlier wording under `edits`,
and one already posted is flagged as having moved on from what the pull request holds rather than silently disagreeing
with it.

A comment also carries where it stands with the pull request, apart from whether it is resolved: `none` when it was
never meant to go there, `pending` while it still owes a post, `failed` after an attempt that did not land (retriable,
with the reason kept), `posted` once it did. The log on disk is written before GitHub is contacted, so a failed post
loses nothing.
"""

import json
import os
import pathlib
import subprocess
import time
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
        row.setdefault("github", "none")
        row.setdefault("replies", [])
        row.setdefault("edits", [])
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
        elif path == "/edit":
            self._edit()
        elif path == "/reply":
            self._reply()
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
        # A batch names whether it is bound for GitHub; a bare list, or a lone comment, is the batch of one.
        if isinstance(body, dict) and "comments" in body:
            batch, bound = body["comments"], bool(body.get("github"))
        else:
            batch, bound = (body if isinstance(body, list) else [body]), False
        rows = read_notes()
        seq = max((row.get("seq", 0) for row in rows), default=0)
        group = max((row.get("batch", 0) for row in rows), default=0) + 1
        for note in batch:
            seq += 1
            note["seq"] = seq
            note["batch"] = group
            note["state"] = "open"
            note["github"] = "pending" if bound else "none"
            note["replies"] = []
            rows.append(note)
        write_notes(rows)
        print(f"BATCH {group}: {len(batch)} comment(s) submitted{', bound for GitHub' if bound else ''}", flush=True)
        for note in batch:
            span = str(note.get("line", "?"))
            if note.get("endLine") and note["endLine"] != note.get("line"):
                span += f"-{note['endLine']}"
            text = " ".join(str(note.get("text", "")).split())
            print(f"  COMMENT [{note['seq']}] {note.get('path', '?')}:{span} :: {text}", flush=True)
        self._json({"ok": True, "batch": group, "seq": seq, "seqs": [note["seq"] for note in batch]})

    def _edit(self):
        """Rewrite a comment, keeping every earlier wording."""
        order = self._body()
        text = (order.get("text") or "").strip()
        rows = read_notes()
        found = next((row for row in rows if row["seq"] == order.get("seq")), None)
        if found is None:
            self._json({"ok": False, "error": f"no comment numbered {order.get('seq')}"})
            return
        if not text:
            self._json({"ok": False, "error": "an empty comment says nothing"})
            return
        found["edits"].append({"at": time.strftime("%H:%M:%S"), "text": found["text"]})
        found["text"] = text
        if found.get("github") == "posted":
            found["editedAfterPost"] = True
        write_notes(rows)
        print(f"EDIT [{found['seq']}] {' '.join(text.split())}", flush=True)
        self._json({"ok": True, "seq": found["seq"], "edits": len(found["edits"])})

    def _reply(self):
        """Add a reply to a comment, from whichever side wrote it. A reply leaves the thread open."""
        order = self._body()
        text = (order.get("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "an empty reply says nothing"})
            return
        who = "you" if order.get("who") == "you" else "session"
        rows = read_notes()
        found = next((row for row in rows if row["seq"] == order.get("seq")), None)
        if found is None:
            self._json({"ok": False, "error": f"no comment numbered {order.get('seq')}"})
            return
        found["replies"].append({"who": who, "text": text, "at": time.strftime("%H:%M:%S")})
        write_notes(rows)
        print(f"REPLY [{found['seq']}] {who}: {' '.join(text.split())}", flush=True)
        self._json({"ok": True, "seq": found["seq"], "replies": len(found["replies"])})

    def _resolve(self):
        """Close comments, or reopen them. Either side may do it, and an answer is kept as a reply of its own."""
        order = self._body()
        wanted = set(order.get("seq") or [])
        answer = (order.get("answer") or "").strip()
        closing = bool(order.get("resolved", True))
        who = "you" if order.get("who") == "you" else "session"
        rows = read_notes()
        touched = 0
        for row in rows:
            if row.get("seq") in wanted:
                row["state"] = "resolved" if closing else "open"
                if answer:
                    row["replies"].append({"who": who, "text": answer, "at": time.strftime("%H:%M:%S")})
                touched += 1
        write_notes(rows)
        print(f"{'RESOLVED' if closing else 'REOPENED'} {touched} comment(s) by {who}", flush=True)
        self._json({"ok": True, "resolved": touched, "state": "resolved" if closing else "open"})

    def _publish(self):
        """Post comments to a pull request as one review, and record where each of them now stands.

        Called with `seq` for a batch just submitted, and without it to clear whatever is still owed, which is what
        makes a post that did not land recoverable rather than lost.
        """
        order = self._body()
        rows = read_notes()
        wanted = set(order.get("seq") or [])
        owed = [row for row in rows if row.get("github") in ("pending", "failed")]
        sending = [row for row in owed if row["seq"] in wanted] if wanted else owed
        if not sending:
            self._json({"ok": True, "sent": 0, "owed": 0})
            return
        review = {"event": "COMMENT", "body": order.get("summary") or "Review from the diff desk.", "comments": []}
        for note in sending:
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
        landed = done.returncode == 0
        url = json.loads(done.stdout or "{}").get("html_url", "") if landed else ""
        error = "" if landed else " ".join((done.stderr or done.stdout).split())[:400]
        marked = {note["seq"] for note in sending}
        for row in rows:
            if row["seq"] in marked:
                row["github"] = "posted" if landed else "failed"
                if landed:
                    row["reviewUrl"] = url
                    row.pop("error", None)
                else:
                    row["error"] = error
        write_notes(rows)
        still = len([row for row in rows if row.get("github") in ("pending", "failed")])
        print(f"{'PUBLISHED ' + url if landed else 'PUBLISH FAILED ' + error} ({still} still owed)", flush=True)
        self._json({"ok": landed, "url": url, "error": error, "sent": len(sending) if landed else 0, "owed": still})

    def log_message(self, *args):
        pass


def main():
    print(f"diff desk on http://127.0.0.1:{PORT}/  (comments -> {NOTES})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
