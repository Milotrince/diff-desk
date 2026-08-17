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

Omit the refs to be offered every local branch ahead of the base. The checked-out branch is shown with its
uncommitted work included, so a review can start before a commit exists. Repository, base and branches can also be
switched from the page's own Source panel, without restarting anything.

On the page:

- **Drag over lines** - anywhere on the line, or over the `+` - to select a range, and let go to open the comment box.
  A range may cover removed and added lines together.
- **`+20 up` / `+20 down` / `all N`** on each hunk header, and `+20 below` at the end of a file, bring in the lines the
  diff left out, read from the file at that branch's revision.
- **Reviewed** folds a file away. The tick is remembered per branch and per file digest, so a file whose diff changes
  reopens itself rather than staying silently ticked.
- **Submit review** sends the whole batch at once, with an optional overall note.
- **Changes only** hides context lines; **Hide reviewed** clears what you are done with; `j`/`k` walk the files, `/`
  filters them, `c` comments on the selection, `r` marks the current file reviewed.

## Handing the comments to a session

The desk records every batch to `~/.claude/diff-desk/comments.jsonl`, numbering each comment and stamping the batch it
arrived in. A session waits for one with:

    python3 desk.py watch

which blocks until a batch lands, prints it as `[seq] branch path:line-endLine (side) text`, and exits. After
addressing them:

    python3 desk.py resolve 3 4 --answer "fixed in abc1234"

The page picks that up on its own and shows those comments closed, with the answer beside them. `desk.py comments`
lists what is still outstanding.

As a Claude Code skill, drop this repository into `~/.claude/skills/diff-desk/` and the flow above needs no
explaining - `SKILL.md` tells the session how to serve a review, wait for comments, and close them out.

## Posting to a pull request

When a branch has an open pull request, the tray offers to post the batch there as well. Ranges become GitHub range
comments; a comment covering removed and added lines is anchored on the added side, which is the side a line range can
be expressed on. It goes out as a single review rather than a stream of separate comments, and it is always opt-in -
comments are recorded locally whether or not GitHub is reachable, which is what makes the desk usable when it is not.

## Layout

| file | what it is |
| --- | --- |
| `desk.py` | the entry point: `serve`, `watch`, `comments`, `resolve`, `refs` |
| `gen_diff_data.py` | turns a git range into the payload a page renders, digests included |
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
