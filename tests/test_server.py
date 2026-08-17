"""The desk's endpoints: what it serves, what it records, and how a session reads the comments back out."""

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import FILE_LINES, SECOND_EDIT


def read(desk, route):
    with urllib.request.urlopen(f"{desk.url}{route}", timeout=30) as answer:
        return answer.status, answer.read()


def test_the_page_and_its_payload_are_served(desk):
    status, body = read(desk, "/")
    assert status == 200
    assert b"<title>" in body and b"__DIFF_DATA__" not in body
    assert desk.get("/data")["branches"][0]["ref"] == "feature"


def test_an_unknown_route_is_refused(desk):
    with pytest.raises(urllib.error.HTTPError) as raised:
        read(desk, "/nowhere")
    assert raised.value.code == 404


def test_branches_are_offered_for_the_picker(desk):
    info = desk.get(f"/refs?dir={urllib.parse.quote(str(desk.repo))}&base=main")
    assert info["current"] == "main"
    assert info["refs"] == [{"ref": "feature", "ahead": 1}]


def test_a_slice_of_the_file_fills_a_gap(desk):
    where = f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=feature&path=sample.py&from=20&to=24"
    answer = desk.get(where)
    assert answer["total"] == FILE_LINES
    assert answer["lines"] == [f"line {number}" for number in range(20, 25)]
    # A revision the page did not ask for must not leak in: the branch's own content is what a gap is filled with.
    edited = desk.get(
        f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=feature&path=sample.py&from={SECOND_EDIT}&to={SECOND_EDIT}"
    )
    assert edited["lines"] == [f"line {SECOND_EDIT} rewritten"]
    ranged = desk.get(
        f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=main&path=sample.py&from={SECOND_EDIT}&to={SECOND_EDIT}"
    )
    assert ranged["lines"] == [f"line {SECOND_EDIT}"]


def test_a_slice_beyond_the_file_is_clamped(desk):
    beyond = FILE_LINES + 40
    where = f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=feature&path=sample.py&from={FILE_LINES}&to={beyond}"
    answer = desk.get(where)
    assert answer["lines"] == [f"line {FILE_LINES}"]
    assert answer["to"] == FILE_LINES


def test_a_batch_is_recorded_numbered_and_read_back_past_a_cursor(desk):
    first = desk.post(
        "/comments",
        [
            {"branch": "feature", "path": "sample.py", "line": 10, "endLine": 12, "side": "new", "text": "the range"},
            {"branch": "feature", "path": "sample.py", "line": 150, "side": "old", "text": "the removal"},
        ],
    )
    assert first["ok"] and first["batch"] >= 1
    rows = desk.get("/comments")
    assert [row["seq"] for row in rows][-2:] == [first["seq"] - 1, first["seq"]]
    assert all(row["batch"] == first["batch"] for row in rows[-2:])
    assert all(row["state"] == "open" for row in rows[-2:])

    # A cursor is what lets a session pick up only what it has not seen.
    assert desk.get(f"/comments?since={first['seq']}") == []
    assert [row["text"] for row in desk.get(f"/comments?since={first['seq'] - 2}")] == ["the range", "the removal"]

    second = desk.post(
        "/comments", {"branch": "feature", "path": "added.py", "line": 1, "side": "new", "text": "alone"}
    )
    assert second["batch"] == first["batch"] + 1
    assert [row["text"] for row in desk.get(f"/comments?since={first['seq']}")] == ["alone"]


def test_resolving_closes_only_what_was_named(desk):
    marked = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 8, "side": "new", "text": "one"})
    other = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 9, "side": "new", "text": "two"})
    outcome = desk.post("/resolve", {"seq": [marked["seq"]], "answer": "done in abc1234"})
    assert outcome == {"ok": True, "resolved": 1}
    rows = {row["seq"]: row for row in desk.get("/comments")}
    assert rows[marked["seq"]]["state"] == "resolved"
    assert rows[marked["seq"]]["answer"] == "done in abc1234"
    assert rows[other["seq"]]["state"] == "open"


def test_the_comments_survive_as_a_readable_log(desk):
    rows = [json.loads(line) for line in (desk.home / "comments.jsonl").read_text().splitlines() if line.strip()]
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert {row["path"] for row in rows} >= {"sample.py", "added.py"}


def test_rescanning_switches_what_is_reviewed(desk):
    outcome = desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["feature"]})
    assert outcome["ok"]
    assert [branch["ref"] for branch in outcome["data"]["branches"]] == ["feature"]
    assert desk.get("/data")["branches"][0]["ref"] == "feature"
    # The page is rebuilt around the new payload, so a reload shows it without another command.
    assert "feature" in (desk.home / "diff_desk.html").read_text()


def test_a_ref_with_nothing_to_review_is_reported_not_served(desk):
    outcome = desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["parked"]})
    assert outcome["ok"] is False
    assert "nothing ahead" in outcome["error"]
    # What was being reviewed stays on screen, so a mistyped scan cannot empty the page.
    assert desk.get("/data")["branches"][0]["ref"] == "feature"


def test_a_branch_behind_the_base_shows_the_difference_it_does_have(desk):
    outcome = desk.post("/scan", {"dir": str(desk.repo), "base": "feature", "refs": ["main"]})
    assert outcome["ok"]
    branch = outcome["data"]["branches"][0]
    assert branch["commits"] == []
    assert branch["files"][0]["path"] == "added.py"
    desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["feature"]})
