"""The page, driven in every engine available: selecting lines, commenting on a range, and filling the gaps.

Pointer handling and sticky positioning differ between engines, so these run per engine rather than once. A missing
engine is skipped, never silently dropped from the run.
"""

import pytest

from conftest import FIRST_EDIT, SECOND_EDIT

playwright = pytest.importorskip("playwright.sync_api")

ENGINES = ("chromium", "webkit", "firefox")
STEP = 20


@pytest.fixture(scope="module")
def play():
    with playwright.sync_playwright() as running:
        yield running


@pytest.fixture(scope="module", params=ENGINES)
def browser(play, request):
    kind = getattr(play, request.param)
    try:
        yield kind.launch()
    except playwright.Error:
        # A machine may carry the system browser rather than the bundled build.
        try:
            yield kind.launch(channel="chrome" if request.param == "chromium" else request.param)
        except playwright.Error as error:
            pytest.skip(f"{request.param} is not installed: {error}")


@pytest.fixture
def page(browser, desk):
    opened = browser.new_page(viewport={"width": 1500, "height": 900})
    problems = []
    opened.on("pageerror", lambda error: problems.append(str(error)))
    opened.goto(f"{desk.url}/", wait_until="load")
    # A file marked reviewed is folded, so readiness is a card being drawn rather than any given row being visible.
    opened.wait_for_selector("section.file")
    opened.wait_for_function("() => document.querySelectorAll('tr[data-line]').length > 0")
    yield opened
    assert problems == []
    opened.close()


def rows(page, kind=""):
    return page.locator(f"tr{kind}[data-line]")


def sample(page):
    """The card of the file both hunks live in, so a range never spans two files by accident."""
    return page.locator("section.file").filter(has=page.locator("text=sample.py")).first


def submit(page, text):
    """Write the comment, add it to the review, and send the batch: recording happens on the send, not the write."""
    page.locator("tr[data-composer='true'] textarea").fill(text)
    page.locator("tr[data-composer='true'] button.solid").click()
    page.wait_for_selector("#tray[data-open='true']")
    page.locator("#traysend").click()
    page.wait_for_function("() => document.getElementById('tray').dataset.open === 'false'")


def drag(page, first, last, column):
    """Press on one row and pull to another, the way a hand does it: in small steps, over the given column.

    A cursor cannot leave the window, so a target below the fold is reached by holding near the edge until the page has
    scrolled it into view, then releasing on it - which is also what exercises the drag's own edge scrolling.
    """
    where = {"pin": "button.pin", "rail": "td.ln", "code": "td.code"}[column]
    # Press on something in view, the way a hand has to: mid-viewport, clear of the header floating over the top.
    first.evaluate("node => node.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(80)
    if column == "pin":
        first.locator("td.code").first.hover()
    start = first.locator(where).first.bounding_box()
    x0, y0 = start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
    tall = page.viewport_size["height"]
    page.mouse.move(x0, y0)
    page.mouse.down()
    for _ in range(60):
        end = last.locator("td.code" if column == "pin" else where).first.bounding_box()
        y1 = end["y"] + end["height"] / 2
        x1 = end["x"] + end["width"] / 2 if column != "pin" else x0
        if 40 < y1 < tall - 40:
            page.mouse.move(x1, y1)
            break
        # Out of view in whichever direction: hold near that edge and let the page scroll the target in.
        page.mouse.move(x0, 20 if y1 <= 40 else tall - 20)
        page.wait_for_timeout(40)
    # What the range holds at the moment of release, which is what the release must not change.
    held = page.locator("tr.sel").count()
    page.mouse.up()
    page.wait_for_timeout(80)
    return held, page.locator("tr.sel").count()


@pytest.mark.parametrize("column", ["pin", "rail", "code"])
@pytest.mark.parametrize("upward", [False, True])
def test_dragging_lines_selects_the_range_and_opens_the_box(page, column, upward):
    lines = rows(page)
    first, last = (lines.nth(9), lines.nth(2)) if upward else (lines.nth(2), lines.nth(9))
    held, kept = drag(page, first, last, column)
    assert held >= 4
    # The release must not shrink what was selected: a trailing click aimed at a line would collapse it.
    assert kept == held
    assert page.locator("tr[data-composer='true']").count() == 1


def test_one_range_covers_removed_and_added_lines_together(page, desk):
    card = sample(page)
    removed = card.locator("tr.d[data-line]").first
    added = card.locator("tr.a[data-line]").first
    drag(page, removed, added, "pin")
    picked = page.evaluate("""() => {
      const chosen = [...document.querySelectorAll('tr.sel')];
      return {removed: chosen.filter((row) => row.classList.contains('d')).length,
              added: chosen.filter((row) => row.classList.contains('a')).length};
    }""")
    assert picked["removed"] >= 1 and picked["added"] >= 1

    # A line range is only expressible on one side, so a range touching added lines is anchored there.
    submit(page, "both sides at once")
    note = desk.get("/comments")[-1]
    assert note["side"] == "new"
    assert note["line"] <= FIRST_EDIT <= (note["endLine"] or note["line"])
    assert note["text"] == "both sides at once"


def test_clicking_one_line_comments_on_that_line_alone(page, desk):
    line = sample(page).locator("tr.a[data-line]").last
    where = int(line.get_attribute("data-line"))
    assert where == SECOND_EDIT
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    assert page.locator("tr.sel").count() == 1
    submit(page, "this line only")
    note = desk.get("/comments")[-1]
    assert note["text"] == "this line only"
    assert note["line"] == where
    assert not note.get("endLine") or note["endLine"] == note["line"]


def test_a_thread_can_be_answered_rewritten_closed_and_reopened_from_the_page(page, desk):
    line = sample(page).locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    submit(page, "the remark as first written")
    seq = desk.get("/comments")[-1]["seq"]
    thread = page.locator(f"#note-{seq}")

    thread.locator("textarea").fill("a reply from the reviewer")
    thread.locator("button.ghost").filter(has_text="Reply").click()
    page.wait_for_function(f"() => document.querySelectorAll('#note-{seq} .reply').length === 1")
    said = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert [(reply["who"], reply["text"]) for reply in said["replies"]] == [("you", "a reply from the reviewer")]

    thread = page.locator(f"#note-{seq}")
    thread.locator("button.tiny").filter(has_text="Edit").click()
    thread.locator("textarea").first.fill("the remark, rewritten")
    thread.locator("button.solid").filter(has_text="Save").click()
    page.wait_for_timeout(400)
    rewritten = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert rewritten["text"] == "the remark, rewritten"
    # Rewriting keeps what it said before, so an edit never silently rewrites history.
    assert [earlier["text"] for earlier in rewritten["edits"]] == ["the remark as first written"]

    page.locator(f"#note-{seq} button.solid").filter(has_text="Resolve").click()
    page.wait_for_timeout(400)
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["state"] == "resolved"
    page.locator(f"#note-{seq} button.ghost").filter(has_text="Reopen").click()
    page.wait_for_timeout(400)
    reopened = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert reopened["state"] == "open"
    # Closing and reopening leave the thread exactly as it was: no reply invented, none dropped.
    assert reopened["text"] == "the remark, rewritten"
    assert [reply["text"] for reply in reopened["replies"]] == ["a reply from the reviewer"]


def test_the_log_says_where_every_comment_stands(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    desk.post(
        "/comments",
        {
            "comments": [
                {"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": "for a PR"}
            ],
            "github": True,
        },
    )
    desk.post(
        "/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": "local"}]
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    marks = set(page.locator("#logrows .mark").all_inner_texts())
    assert "waiting for GitHub" in marks
    assert "local only" in marks
    # What is owed is offered for sending, rather than being discoverable only in a log file.
    assert page.locator("#logretry").is_enabled()
    assert "waiting" in page.locator("#logopen").inner_text()
    page.locator("#logclose").click()


def test_a_comment_whose_line_left_the_diff_is_kept_and_marked(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    desk.post(
        "/comments",
        [{"branch": branch, "path": "sample.py", "line": 9999, "side": "new", "text": "anchored to a vanished line"}],
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    page.wait_for_function("() => document.querySelectorAll('.thread.stale').length > 0")
    stale = page.locator(".thread.stale").first
    # Kept with its file and marked, never dropped from the page and never resolved on its behalf.
    assert "anchored to a vanished line" in stale.inner_text()
    assert stale.locator(".mark.outdated").first.inner_text() == "code moved on"
    kept = next(row for row in desk.get("/comments") if row["line"] == 9999)
    assert kept["state"] == "open"


def test_a_release_the_page_never_sees_does_not_leave_it_dragging(page):
    line = rows(page).nth(2)
    line.locator("td.code").first.hover()
    box = line.locator("button.pin").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 60)
    assert page.locator("body.dragging").count() == 1
    # A release outside the window is never delivered; the next motion with no button held has to end the drag, or
    # every later hover keeps extending a range nobody is holding.
    page.evaluate("""() => document.dispatchEvent(
      new PointerEvent('pointermove', {clientX: 40, clientY: 40, buttons: 0, bubbles: true})
    )""")
    assert page.locator("body.dragging").count() == 0
    page.mouse.up()


def test_selecting_text_inside_a_line_stays_a_text_selection(page):
    box = rows(page).nth(4).locator("td.code").first.bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + box["height"] / 2)
    page.mouse.down()
    for step in range(1, 11):
        page.mouse.move(box["x"] + 20 + step * 12, box["y"] + box["height"] / 2)
    page.mouse.up()
    assert page.locator("tr.sel").count() <= 1
    assert page.locator("tr[data-composer='true']").count() == 0


def test_a_gap_can_be_filled_and_leaves_no_bare_delimiter(page):
    card = page.locator("section.file").filter(has=page.locator("text=sample.py")).first
    before = card.locator("tr.c").count()
    gap = card.locator("button.expand").first
    assert "+" in gap.inner_text() or "all" in gap.inner_text()
    gap.click()
    page.wait_for_timeout(600)
    assert card.locator("tr.c").count() > before
    # A line brought in is the file's own, numbered identically on both sides, which is what makes it commentable.
    brought = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('section.file')];
      const card = cards.find((node) => node.textContent.includes('sample.py'));
      const row = card.querySelector('tr.c[data-line]');
      return {
        side: row.dataset.side,
        numbers: [...row.querySelectorAll('td.ln')].map((cell) => cell.textContent),
        text: row.querySelector('td.code').textContent,
      };
    }""")
    assert brought["side"] == "new"
    assert brought["numbers"] == [brought["numbers"][0]] * 2
    assert brought["text"] == f"line {brought['numbers'][0]}"
    bare = page.evaluate(
        """() => [...document.querySelectorAll('tr.h')].filter((row) => !row.querySelector('button')).length"""
    )
    assert bare == 0


def test_expanding_every_gap_reaches_the_whole_file(page):
    card = page.locator("section.file").filter(has=page.locator("text=sample.py")).first
    for _ in range(12):
        buttons = card.locator("button.expand")
        if not buttons.count():
            break
        buttons.first.click()
        page.wait_for_timeout(400)
    shown = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('section.file')];
      const card = cards.find((node) => node.textContent.includes('sample.py'));
      const seen = new Set();
      for (const row of card.querySelectorAll('tr[data-side="new"]')) seen.add(Number(row.dataset.line));
      return {lines: seen.size, first: Math.min(...seen), last: Math.max(...seen)};
    }""")
    assert shown["first"] == 1
    assert shown["lines"] >= SECOND_EDIT


def test_the_source_panel_lists_what_can_be_reviewed(page):
    page.locator("#source > summary").click()
    page.wait_for_selector("#srcrefs label")
    listed = page.locator("#srcrefs label")
    assert listed.count() >= 1
    assert "feature" in listed.first.inner_text()
    # The branch under review is already ticked, so a rescan does not silently drop it.
    assert listed.first.locator("input").is_checked()
    # Pull requests are listed only where there are any, so a plain local repository shows branches alone.
    assert page.locator("#srcpulls label").count() == 0
    page.locator("#srcfilter").fill("nothing matches this")
    assert page.locator("#srcrefs label").count() == 1
    page.locator("#srcfilter").fill("")


@pytest.mark.parametrize("width", [1500, 1000, 760])
def test_the_pinned_file_head_clears_the_page_header(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      const cards = [...document.querySelectorAll('section.file')];
      const tall = cards.reduce((one, other) => (other.offsetHeight > one.offsetHeight ? other : one));
      window.scrollTo(0, tall.offsetTop + tall.offsetHeight / 2);
    }""")
    page.wait_for_timeout(250)
    look = page.evaluate("""() => {
      const header = document.querySelector('header').getBoundingClientRect();
      const pinned = [...document.querySelectorAll('.filehead')]
        .map((node) => node.getBoundingClientRect())
        .filter((box) => box.top >= -1 && box.top < 260)
        .sort((one, other) => one.top - other.top)[0];
      const under = document.elementFromPoint(Math.round(header.width / 2), Math.round(header.bottom + 4));
      return {
        bottom: header.bottom,
        top: pinned ? pinned.top : null,
        head: Boolean(under && under.closest('.filehead')),
      };
    }""")
    assert look["top"] is not None
    # A head hidden behind the page header leaves a hunk delimiter standing where the file name should be.
    assert look["top"] >= look["bottom"] - 1
    assert look["head"]


def test_marking_a_file_reviewed_is_remembered_across_reloads(page):
    card = sample(page)
    card.locator("input[type=checkbox]").check()
    page.wait_for_timeout(150)
    assert card.get_attribute("data-done") == "true"
    # Reviewed folds the diff away, which is the point of the tick.
    assert card.get_attribute("data-open") == "false"
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    again = sample(page)
    assert again.get_attribute("data-done") == "true"
    again.locator("input[type=checkbox]").uncheck()
    page.wait_for_timeout(150)
    assert again.get_attribute("data-done") == "false"
    page.evaluate("() => localStorage.clear()")
