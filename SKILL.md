---
name: diff-desk
description: Serve a local git branch as a browser diff review page, collect the line comments the user leaves on it, and work through them. Use when the user wants to review a branch, diff or pull request locally, when GitHub is unreachable, or when they refer to comments they left in the review tool.
---

# Diff desk

A local review page for any git range: side-by-side line numbers, per-commit or whole-branch scope, reviewed-file tracking, range comments dragged over any lines, and an optional batch post to the branch's pull request. Needs `git`, `gh` and python3 only. The page is served on `http://127.0.0.1:8787/`; its payload, page and comments live in `~/.claude/diff-desk/` (override with `DIFF_DESK_HOME`).

## Serving a review

Run in the background, then give the user the URL:

    python3 ~/.claude/skills/diff-desk/desk.py serve --dir <repo> --base <ref> [refs ...]

Serving fast-forwards the desk itself to what has been published and restarts into it, so a review always runs the current tool. It only ever fast-forwards a clean `main`: a checkout carrying work in progress, or sitting on another branch, is left alone and says so - which is what a session sees when it is serving from a working copy being changed.

`refs` are local branches, pull request numbers (`3243`, `#3243`, `pr/3243`), or a mix of both; omit them to offer every local branch ahead of the base. `--base` defaults to `upstream/main`. The checked-out branch is shown with its uncommitted work included. A pull request is fetched from the upstream repository by number into `refs/diffdesk/pull/<number>`, so the fork and branch it lives on never have to be named. Re-running while a desk is already up just rebuilds the page. The user can also switch repository, base, branches and pull requests from the page's own Source panel.

A page load collects the diffs again, so a reload always shows the branch as it now stands, uncommitted work included. While the page is open it watches what it was built from and offers a **Refresh** in the bar once that has moved on - a commit, a fixup, work saved on disk - rather than rebuilding the diff under the reader. Taking it keeps the branch and commit being read. So an edit made while the user is reviewing needs no command from the session; tell them to refresh.

`desk.py refs --dir <repo> --base <ref>` lists what is available: branches ahead of the base, and open pull requests.

## Picking up the comments

The user writes comments on the page and presses "Submit review", which sends the whole batch at once. Nothing has to be
armed to hear about it: the Stop hook, `on_stop.py`, runs whenever a session would have gone idle, finds the desk
serving the tree it is working in, and hands over whatever the reviewer has said. So a batch reaches a session by being
submitted, and the session's job is to work through what the hook gives it.

Installed in `~/.claude/settings.json`, which the README spells out:

    "Stop": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/skills/diff-desk/on_stop.py",
      "asyncRewake": true, "timeout": 1800}]}]

If a session is picking up comments only when the user asks it to, that hook is missing.

Several desks at once are the normal case - a review per branch, or per worktree - and each is a review of its own: its
own port, its own home, its own comments and its own place in them. The hook waits on all of the session's desks at the
same time and hands over whichever speaks first, so which one the reviewer comments on does not matter. A desk belongs
to the session that started it, so two sessions reviewing different branches of one repository are never handed each
other's comments; one started by hand belongs to whoever is working in the tree it serves.

To wait for a batch by hand - a session already at work, wanting the next word before it stops:

    python3 ~/.claude/skills/diff-desk/desk.py watch

It blocks until the reviewer says something, prints it, and exits, because a background command that does not exit
tells a session nothing. Its exit code says how it went: 0 heard something, 3 found no desk running or one restarted
since the watch was armed, 4 waited out its `--timeout`. `--follow` keeps it printing instead, for a terminal a person
is reading.

What it heard goes to stdout and how the watch itself went to stderr, so a report sent to a file is a report nobody
reads. Either keep the stream where it will be read, or let it exit and read what it printed.

What it follows is what the reviewer has said on each thread, not the comment numbers, so a reply on a comment already
read reaches it just the same. A thread is printed whole with the line that woke the session marked by a `*`, so what
has already been answered reads as read. Where it stopped is remembered, so the same word never arrives twice and
nothing said while the session was answering is lost. `--since N` overrides where it resumes from.

Each comment prints as `[seq] branch path:line-endLine (side) text`, followed by its state and any replies, each numbered `[0]`, `[1]` ... by its place in the thread. Answer in the thread, and close what is done - the page shows both without a reload:

    python3 ~/.claude/skills/diff-desk/desk.py reply 3 "it happens because ..."
    python3 ~/.claude/skills/diff-desk/desk.py reply 3 --note "check the sibling call site too"
    python3 ~/.claude/skills/diff-desk/desk.py forget 3
    python3 ~/.claude/skills/diff-desk/desk.py resolve 3 4 --answer "fixed in abc1234"
    python3 ~/.claude/skills/diff-desk/desk.py resolve 3 --reopen
    python3 ~/.claude/skills/diff-desk/desk.py edit 3 "what I actually meant ..."
    python3 ~/.claude/skills/diff-desk/desk.py edit 3 --reply 0 "worded better ..."

Reply when the answer needs discussing, resolve when it is settled - a resolved thread keeps its remark and every reply, and the reviewer can reopen it. A note is neither: printed as `[i] note <who> ...` in a thread, it is written for this side of the desk - what the reviewer wants looked at, what is left to do - and it never reaches the pull request, so it is guidance to act on rather than a remark to answer. It hangs on what it is about, said as `on [j]` when that is not the remark itself, and it is answered with a note of its own: a reviewer's "check whether this is true, and if it is not just resolve it" is answered with `reply 3 --note --on 0 "done."` and the thread closed with `resolve 3`, which leaves nothing of it on the pull request. A note stands on the remark or on a reply, and every comment carries one way to add another about it. `forget` lets go of the last note on a comment, which is the only one that can go: what stands under it would be left standing against nothing. Resolving a comment that was posted to a pull request also resolves its thread there, and says "not resolved there yet" until GitHub confirms it.

Nothing said here has left the desk until the reader sends the thread, so an answer that turns out to be wrong is rewritten rather than corrected underneath: `edit <seq> --reply <i>` keeps the earlier wording and leaves the thread reading as one answer. Append a correction only to what the pull request already holds.

`desk.py sync` brings back what the pull request holds: replies added there, its word on what is resolved, and the comments written there that this desk has no record of - a reviewer's remark, a bot's report - each numbered like any other and carrying its author. Run it when the reviewer mentions having answered on GitHub, or before working through comments, so this desk holds everything they have said. `desk.py comments [--all]` lists what is outstanding.

A comment brought in that way is answered and resolved like any other, and is the one kind that cannot be reworded or deleted from here: the remark is its author's, on the copy everyone reads.

Nothing a session writes reaches the pull request. `desk.py sync` only brings back what the pull request holds; sending a thread out - the remark, the replies it does not hold, its resolution - is a press on that thread's **Sync**, on the thread itself or on its row in the Comments panel, and the reader's alone. The panel also sends every thread already on the pull request at once. So working through a review leaves no trace there unless they put it there, and a reply written after they sent a thread waits for them to send it again - a resolution too: closing a thread here is local until sent.

## Behaviour to know

- A comment range may cover removed and added lines together. It is anchored to the added side when the range touches it, so `side`/`line`/`endLine` are always expressible as a GitHub line range.
- The page holds the files the reader is near: a file further off keeps its header and stands at the height its lines will take, and its rows are built when they come within reach. So a query about what is in the DOM is a question about where the reader is standing, and a test reaching into a distant file scrolls to it first.
- A batch is a submission, not a grouping of comments: it says which review a remark went out in, and a reply belongs to its thread and carries no batch at all. The Comments panel therefore reads either way - by batch, or by what moved last, which is where an answer written into an old thread is found.
- Reviewed-file ticks are remembered per branch and per file digest, so a file whose diff changes reopens by itself.
- Gap expanders on each hunk header read the file at the branch revision, so context beyond the diff needs the desk running (they are hidden otherwise).
- Comments are recorded whether or not GitHub is reachable; posting to a pull request is a separate opt-in tick. A post that does not land leaves its comments marked as still owed, with the reason kept, and they are retried from the page - so a GitHub outage never costs a comment and never needs cleaning up by hand. A comment GitHub rejects outright is marked `refused` rather than retried forever.
- A comment can be sent on its own from the box, sent alone out of the review tray, or batched into a review; the Comments panel groups comments by batch and sends them one batch at a time. All of it is recorded identically.
- Whether a comment is bound for the pull request can be changed after it was recorded, from the page or with `desk.py bind <seq...> [--local]`, for as long as it has not landed.
- A comment whose line has left the diff is kept and marked, never resolved or dropped on the reviewer's behalf.

## Changing the page

`diff_desk_template.html` holds the page, with `__DIFF_DATA__` and `__BUILD__` substituted at build time. After editing it, verify with the suite rather than by inspection - it drives the real page in Chromium, WebKit and Firefox against a desk of its own, and covers the drag, mixed ranges, gap expansion, the single-click paths, the layout under a comment, and every exchange with a stand-in for `gh`:

    cd ~/.claude/skills/diff-desk && python3 -m pytest

Pointer behaviour differs between engines, so a change to selection or hit testing is not done until it passes in all three. The header carries a build stamp; if the user reports stale behaviour, have them compare it first.

Three traps the suite exists to catch, all of which shipped broken before it did:

- An `overflow` on the file card makes the card its own scrollport, so its head never pins and the hunk delimiter is what stands at the top of the view, reading as the file's name.
- A trailing click follows every drag, aimed at the pin or at an ancestor depending on the engine, and collapses the range to one line unless it is swallowed.
- A comment hangs inside the diff table, so one left without a width of its own fills the table and moves every column under it - which the page redrawing itself turns into a flicker.
