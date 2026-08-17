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
    """Press on one row and pull to another, the way a hand does it: in small steps, over the given column."""
    where = {"pin": "button.pin", "rail": "td.ln", "code": "td.code"}[column]
    if column == "pin":
        first.locator("td.code").first.hover()
    start = first.locator(where).first.bounding_box()
    end = last.locator("td.code" if column == "pin" else where).first.bounding_box()
    x0, y0 = start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
    x1, y1 = end["x"] + end["width"] / 2, end["y"] + end["height"] / 2
    page.mouse.move(x0, y0)
    page.mouse.down()
    held = 0
    steps = max(8, int(abs(y1 - y0) / 3))
    for step in range(1, steps + 1):
        page.mouse.move(x0 + (x1 - x0) * step / steps, y0 + (y1 - y0) * step / steps)
        held = max(held, page.locator("tr.sel").count())
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
