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


def test_pull_requests_are_offered_beside_the_branches(desk):
    info = desk.get(f"/refs?dir={urllib.parse.quote(str(desk.repo))}&base=main")
    # A repository with no GitHub remote has no pull requests to offer, and says so rather than omitting the key.
    assert info["pulls"] == []
    assert info["upstream"] == ""


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


def test_resolving_closes_only_what_was_named_and_can_be_undone(desk):
    marked = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 8, "side": "new", "text": "one"})
    other = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 9, "side": "new", "text": "two"})
    outcome = desk.post("/resolve", {"seq": [marked["seq"]], "answer": "done in abc1234"})
    assert outcome["resolved"] == 1
    rows = {row["seq"]: row for row in desk.get("/comments")}
    closed = rows[marked["seq"]]
    assert closed["state"] == "resolved"
    # Closing keeps the remark and files the answer as a reply of its own, so nothing about it is lost.
    assert closed["text"] == "one"
    assert [(reply["who"], reply["text"]) for reply in closed["replies"]] == [("session", "done in abc1234")]
    assert rows[other["seq"]]["state"] == "open"

    reopened = desk.post("/resolve", {"seq": [marked["seq"]], "resolved": False, "who": "you"})
    assert reopened["state"] == "open"
    again = {row["seq"]: row for row in desk.get("/comments")}[marked["seq"]]
    assert again["state"] == "open"
    assert len(again["replies"]) == 1


def test_either_side_can_reply_without_closing_the_thread(desk):
    made = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 11, "side": "new", "text": "why?"})
    assert desk.post("/reply", {"seq": made["seq"], "text": "because of X", "who": "session"})["replies"] == 1
    assert desk.post("/reply", {"seq": made["seq"], "text": "then what about Y", "who": "you"})["replies"] == 2
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert [(reply["who"], reply["text"]) for reply in row["replies"]] == [
        ("session", "because of X"),
        ("you", "then what about Y"),
    ]
    assert row["state"] == "open"
    assert desk.post("/reply", {"seq": made["seq"], "text": "   "})["ok"] is False
    assert desk.post("/reply", {"seq": 99999, "text": "nowhere"})["ok"] is False


def test_rewriting_a_comment_keeps_what_it_said_before(desk):
    made = desk.post(
        "/comments", {"branch": "feature", "path": "sample.py", "line": 12, "side": "new", "text": "first"}
    )
    assert desk.post("/edit", {"seq": made["seq"], "text": "second"})["edits"] == 1
    assert desk.post("/edit", {"seq": made["seq"], "text": "third"})["edits"] == 2
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert row["text"] == "third"
    assert [earlier["text"] for earlier in row["edits"]] == ["first", "second"]
    assert desk.post("/edit", {"seq": made["seq"], "text": " "})["ok"] is False


def test_a_comment_bound_for_github_waits_rather_than_being_lost(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 13, "side": "new", "text": "for the PR"}],
            "github": True,
        },
    )
    rows = {row["seq"]: row for row in desk.get("/comments")}
    assert rows[made["seqs"][0]]["github"] == "pending"

    # A repository that cannot take it stands in for every way a post fails: no network, no permission, wrong slug.
    outcome = desk.post("/publish", {"repo": "duburcqa/no-such-repo-at-all", "pr": 1, "seq": made["seqs"]})
    assert outcome["ok"] is False
    assert outcome["sent"] == 0
    kept = {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]
    assert kept["github"] == "failed"
    assert kept["text"] == "for the PR"
    assert kept["error"]

    # Retrying takes everything still owed without being told which, which is what makes a failure recoverable.
    again = desk.post("/publish", {"repo": "duburcqa/no-such-repo-at-all", "pr": 1})
    assert again["owed"] >= 1
    assert {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]["github"] == "failed"
    # A sequence nobody owes anything for is not a post at all.
    assert desk.post("/publish", {"repo": "duburcqa/no-such-repo-at-all", "pr": 1, "seq": [99999]}) == {
        "ok": True,
        "sent": 0,
        "owed": 0,
    }


def test_a_comment_not_bound_for_github_is_never_offered_to_it(desk):
    made = desk.post(
        "/comments", [{"branch": "feature", "path": "sample.py", "line": 14, "side": "new", "text": "local"}]
    )
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert row["github"] == "none"
    # Publishing everything owed must leave a comment that was never meant for the pull request alone.
    desk.post("/publish", {"repo": "duburcqa/no-such-repo-at-all", "pr": 1})
    assert {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]["github"] == "none"


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
