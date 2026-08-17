# Diff desk

A local code review page for any git range, and a way to hand the comments you leave on it to an AI coding session.

Point it at a repository and a base ref; it serves a review page on `127.0.0.1:8787` with your branches as tabs, each
scoped to the whole range or to a single commit. You comment on lines by dragging over them; every submitted batch is
recorded to a file with a cursor, so a session can pick up exactly what it has not seen yet, work through it, and mark
each comment addressed - which the page then shows as closed. If the branch has an open pull request, the same batch
can optionally be posted there as one review.

It needs `git`, `gh` and python3. Nothing else - no build step, no packages, no service.

## Using it

    python3 desk.py serve --dir /path/to/repo --base upstream/main [refs ...]

What can be reviewed:

- **Local branches.** Omit the refs entirely to be offered every local branch ahead of the base. The checked-out
  branch is shown with its uncommitted work included, so a review can start before a commit exists.
- **Pull requests, by number** - `3243`, `#3243` or `pr/3243`. The head is fetched from the upstream repository by
  number, so neither the fork it lives on nor the branch name it uses has to be known, and a head force-pushed since
  the last look is picked up. It lands in `refs/diffdesk/pull/<number>`, out of the way of your own branches.

Both can be reviewed side by side in one desk, as tabs. Repository, base, branches and pull requests can also be
switched from the page's own Source panel, without restarting anything - it lists the branches ahead of the base and
every open pull request, filterable together.

On the page:

- **Drag over lines** - anywhere on the line, or over the `+` - to select a range, and let go to open the comment box.
  A range may cover removed and added lines together.
- **`+20 up` / `+20 down` / `all N`** on each hunk header, and `+20 below` at the end of a file, bring in the lines the
  diff left out, read from the file at that branch's revision.
- **Reviewed** folds a file away. The tick is remembered per branch and per file digest, so a file whose diff changes
  reopens itself rather than staying silently ticked.
- **Send** in the comment box posts that one comment immediately; **Add to review** keeps it for a batch that goes out
  together with **Submit review** and an optional overall note. Both take the same path, so a lone comment is recorded,
  threaded and posted exactly like a batch.
- **Every comment is a thread.** Either side can reply, and either side can resolve or reopen it. Resolving folds the
  thread to its remark alone and keeps every reply behind one click; nothing is ever deleted.
- **Edit** rewrites a comment and keeps what it said before. One already posted to a pull request is marked as having
  moved on from what the pull request holds.
- **Comments** in the header opens the log: every comment on the branch, whether it is open or resolved, whether it is
  waiting for GitHub, already on the pull request, or local only. Clicking one jumps to it.

A comment whose line is no longer in the diff - the branch moved on, the code was rewritten - is **kept**, marked "code
moved on", and shown at the end of the file it belonged to. It is never resolved or deleted on your behalf.
- **The file list is a tree** following the repository's folders, each foldable and remembered across reloads, with a
  count per folder. A chain of single-child directories is one row, so a deep path costs one line and not one per
  level. Walking onto a file inside a folded folder reveals it.
- **Changes only** hides context lines; **Hide reviewed** clears what you are done with; `j`/`k` walk the files, `/`
  filters them, `c` comments on the selection, `r` marks the current file reviewed.

## Handing the comments to a session

The desk records every batch to `~/.claude/diff-desk/comments.jsonl`, numbering each comment and stamping the batch it
arrived in. A session waits for one with:

    python3 desk.py watch

which blocks until a batch lands, prints each comment as `[seq] branch path:line-endLine (side) text` with its state
and any replies, and exits. A session then answers, closes, or rewrites them:

    python3 desk.py reply 3 "it happens because ..."          # answer, leaving it open
    python3 desk.py resolve 3 4 --answer "fixed in abc1234"   # answer and close
    python3 desk.py resolve 3 --reopen                        # put one back
    python3 desk.py edit 3 "what I actually meant ..."        # rewrite, keeping the earlier wording

The page picks all of that up on its own, threading the replies under the comment. `desk.py comments` lists what is
still outstanding, with each thread's replies and where it stands with GitHub.

As a Claude Code skill, drop this repository into `~/.claude/skills/diff-desk/` and the flow above needs no
explaining - `SKILL.md` tells the session how to serve a review, wait for comments, and close them out.

## Posting to a pull request

When a branch has an open pull request, the tray offers to post the batch there as well. Ranges become GitHub range
comments; a comment covering removed and added lines is anchored on the added side, which is the side a line range can
be expressed on. It goes out as a single review rather than a stream of separate comments, and it is always opt-in.

**A post that does not land loses nothing.** The comment is written to the log before GitHub is contacted, so the order
of events is: recorded, then attempted. A comment bound for a pull request is marked `pending` until it lands and
`failed` if it does not, keeping the reason. Either way it stays in the log, and the log panel shows exactly how many
are waiting.

What is waiting is retried three ways: the button in the log panel, on its own every few seconds while the page is
open, and the moment the browser reports the network is back. Retrying takes everything still owed without being told
which, so a failure needs no bookkeeping from you. A comment never meant for the pull request is left alone by all of
it.

A refusal is told apart from a failure: when GitHub rejects the comment itself - a line outside the diff, a pull
request that is gone, no permission - retrying cannot help, so it is marked `refused` with its reason and left in the
log instead of being attempted forever.

## Layout

| file | what it is |
| --- | --- |
| `desk.py` | the entry point: `serve`, `watch`, `comments`, `resolve`, `refs` |
| `gen_diff_data.py` | turns a git range into the payload a page renders: hunks, digests, pull request resolution |
| `serve_diff.py` | the local server: the page, rescans, file slices, comments, resolutions, pull request posts |
| `diff_desk_template.html` | the page itself, with `__DIFF_DATA__` and `__BUILD__` substituted at build time |
| `SKILL.md` | how a Claude Code session drives all of the above |

State lives in `~/.claude/diff-desk/` (override with `DIFF_DESK_HOME`); the port is `DIFF_DESK_PORT`.

## Development

    pip install pytest playwright && playwright install chromium firefox webkit
    python3 -m pytest

The suite builds its own repository to review and its own desk to serve it, so it touches neither your checkouts nor
the network. `tests/test_page.py` drives the page in Chromium, WebKit and Firefox because pointer handling and sticky
positioning genuinely differ between them - two defects that shipped here were invisible in two engines out of three:

- A trailing click follows every drag, aimed at the pin or at an ancestor depending on the engine, and collapses the
  range to a single line unless it is swallowed.
- An `overflow` on a file card makes the card its own scrollport, so its head never pins and the hunk delimiter is
  what stands at the top of the view, reading as the file's name.

A change to selection, hit testing or layout is not done until the suite passes in all three.
