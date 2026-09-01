"""Hand a session the comments the reviewer just submitted, at the moment it would otherwise have gone idle.

Claude Code's only way of reaching a session that has stopped is a Stop hook that refuses the stop, so this is where a
review picks itself up. Installed as one, it finds the desk serving the repository the session is working in, asks it
for anything the reviewer has said, and hands that back - which puts the session straight back to work on it.

    {"type": "command", "command": "python3 ~/.claude/skills/diff-desk/on_stop.py",
     "asyncRewake": true, "timeout": 1800}

Nothing to arm, nothing to remember: a watch has to be started again after every batch, and the batch that lands while
nobody thought to do so is the one the reviewer has to ask about by hand.

`asyncRewake` is what makes waiting free: the hook goes to the background, the session goes idle as it always did, and
a comment submitted twenty minutes later still wakes it. So it waits DIFF_DESK_WAIT seconds (1500 by default; 0 to hand
over only what has already landed) - within the hook's own timeout, which is what really bounds it.

It waits only while somebody has the review open, since a page nobody is reading is a review nobody is about to comment
on; a page unheard from for DIFF_DESK_LOOK seconds (120 by default) counts as closed. And one waiter per desk at a
time, so a session that stops ten times does not leave ten of them polling.
"""

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

# A hook is run from wherever the session is, not from here, so the desk it ships beside has to be put on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import gen_diff_data

HERE = pathlib.Path(__file__).resolve().parent
# What a watch says on the way out. Kept here rather than imported, since importing desk.py binds a port from the
# environment and this hook addresses every desk in turn.
HEARD, GONE, NOTHING = 0, 3, 4


def about(port, route):
    """One question to a desk, or None when it is not answering."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=5) as answer:
            return json.loads(answer.read() or b"null")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def related(root, cwd):
    """Whether that desk is serving the tree this session is working in, either being inside the other.

    A worktree is reviewed from the repository it hangs off as often as from itself, and a session sitting at the
    repository is working on both.
    """
    root, cwd = pathlib.Path(root), pathlib.Path(cwd)
    return root == cwd or root in cwd.parents or cwd in root.parents


def loose(desk):
    """Whether no session is holding that desk: it names none, or the session that started it has since exited."""
    return not desk.get("owner") or not gen_diff_data.alive(desk.get("ownerPid"))


def ours(cwd, me):
    """The desks this session should be told about: the ones it started, or failing that the ones serving its tree.

    Which session started a desk is the one exact answer to whose review it is, and the tree being served is not: two
    sessions reviewing different branches of one repository, or a worktree and the repository it hangs off, each read
    as serving the other's desk. So a desk another session is holding is that session's to answer and is never taken
    here. One that nobody is holding - run by hand in a terminal, or left behind by a session that has exited -
    belongs to whoever is working where it points.
    """
    desks = gen_diff_data.running()
    started = [desk for desk in desks if me and desk.get("owner") == me]
    if started:
        return started
    return [desk for desk in desks if loose(desk) and related(desk.get("root", ""), cwd)]


def watching(desk):
    """A watch on that desk, waiting the seconds the desk was given and saying nothing until it has something."""
    return subprocess.Popen(
        [
            sys.executable,
            str(HERE / "desk.py"),
            "watch",
            "--timeout",
            str(max(desk["wait"], 1)),
            # A comment is worth hearing about within a few seconds, and this may be polling for twenty minutes.
            "--every",
            "3",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "DIFF_DESK_PORT": str(desk["port"]), "DIFF_DESK_HOME": desk["home"]},
    )


def hear(desks):
    """Watch every one of them at once, and hand back whichever speaks first with the desk it came from.

    One watch per desk rather than one on the likeliest: a reviewer with two reviews open comments on either, and a
    session waiting on only one of them would hear about the other on its next stop - which, for a session that has
    gone idle, never comes.
    """
    watches = {}
    for desk in desks:
        if claim(desk):
            watches[watching(desk)] = desk
    try:
        while watches:
            for watch in [held for held in watches if held.poll() is not None]:
                desk = watches.pop(watch)
                release(desk)
                said, trouble = watch.communicate()
                if watch.returncode == HEARD:
                    return desk, said
                if watch.returncode == GONE:
                    gen_diff_data.unregister(desk["port"], desk["pid"])
                elif watch.returncode != NOTHING:
                    # A watch that fell over is not a reviewer with nothing to say, and a hook that fails quietly is
                    # the silence this one exists to end. Said out loud, without touching the stop.
                    trouble = " ".join(trouble.split())[:300]
                    print(f"diff desk: the watch on port {desk['port']} failed: {trouble}", file=sys.stderr)
            time.sleep(0.4)
    finally:
        for watch, desk in watches.items():
            watch.kill()
            release(desk)
    return None, ""


def waiter(desk):
    # Not a .json, which is what the addresses are: this sits in the same directory and must not read as a desk.
    return gen_diff_data.running_dir() / f"waiting-{desk['port']}.lock"


def claim(desk):
    """Take the one waiting place at this desk, or say that it is taken.

    A session stops many times over a review, and every stop runs this. Without a place to claim, each one leaves
    another waiter polling the same desk and sharing the same cursor, of which exactly one can ever report anything.
    """
    held = waiter(desk)
    try:
        if gen_diff_data.alive(json.loads(held.read_text())["pid"]):
            return False
    except (OSError, ValueError, KeyError):
        pass
    held.write_text(json.dumps({"pid": os.getpid()}))
    return True


def release(desk):
    waiter(desk).unlink(missing_ok=True)


def report(desk, said):
    """Hand the session the comments and where to answer them, and refuse the stop so it reads them."""
    where = f"DIFF_DESK_PORT={desk['port']} DIFF_DESK_HOME={desk['home']} python3 {HERE / 'desk.py'}"
    print(
        f"The reviewer has submitted comments on the diff desk serving {desk['root']}.\n\n{said.strip()}\n\n"
        f"Work through them now: answer each one in its thread and close what is done, with\n"
        f"  {where} reply <seq> '...'\n"
        f"  {where} resolve <seq> --answer '...'\n"
        f"and read what is outstanding with {where} comments.",
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    try:
        asked = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        asked = {}
    # The environment first, since that is the same thing the desk recorded; what the hook is handed covers a harness
    # that does not export it.
    me = gen_diff_data.owner() or asked.get("session_id") or ""
    mine = ours(asked.get("cwd") or os.getcwd(), me)
    if not mine:
        return
    wait = float(os.environ.get("DIFF_DESK_WAIT", "1500"))
    look = float(os.environ.get("DIFF_DESK_LOOK", "120"))
    for desk in mine:
        reading = (about(desk["port"], "/state") or {}).get("reading") or 0
        # A desk being read is waited on; one whose page nobody has open is asked what has already landed and left
        # alone, since a review nobody is looking at is not about to be commented on.
        desk["wait"] = wait if time.time() - reading < look else 0
    desk, said = hear(mine)
    if desk is not None:
        report(desk, said)


if __name__ == "__main__":
    main()
