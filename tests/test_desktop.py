"""The macOS bundle: it must be well-formed, and it must shut down cleanly.

The shutdown half matters more than it sounds. Stockfish runs as child
*processes*, so an app that exits without closing them leaves orphans holding
memory until someone notices them in Activity Monitor - the exact failure a
double-clickable wrapper is prone to.
"""

import plistlib
import socket
import sys

import pytest

from coach.desktop import build_app, launcher

# ------------------------------------------------------------------ port choice

def test_prefers_the_default_port_when_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError:
            pytest.skip("cannot bind a local socket here")
        free = probe.getsockname()[1]
    assert launcher.find_port(free) == free


def test_falls_back_to_another_port_when_the_default_is_taken():
    """Two copies open at once, or a stray `coach serve`, must not be fatal."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        chosen = launcher.find_port(busy)
        assert chosen != busy
        assert 1024 < chosen < 65536


# --------------------------------------------------------------- engine cleanup

def test_shutdown_closes_every_registered_engine():
    from coach.sparring import backend

    class FakeEngine:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    engines = [FakeEngine() for _ in range(3)]
    for e in engines:
        backend.register_engine(e)
    assert launcher.shutdown_engines() >= 3
    assert all(e.quit_called for e in engines)
    assert not backend._OPEN_ENGINES


def test_an_engine_that_fails_to_quit_does_not_block_the_others():
    from coach.sparring import backend

    class Stubborn:
        def quit(self):
            raise RuntimeError("wedged")

    class Fine:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    good = Fine()
    backend.register_engine(Stubborn())
    backend.register_engine(good)
    launcher.shutdown_engines()
    assert good.quit_called
    assert not backend._OPEN_ENGINES


def test_unregistering_prevents_a_double_quit():
    from coach.sparring import backend

    class FakeEngine:
        def __init__(self):
            self.quits = 0

        def quit(self):
            self.quits += 1

    e = FakeEngine()
    backend.register_engine(e)
    backend.unregister_engine(e)
    launcher.shutdown_engines()
    assert e.quits == 0


def test_shutdown_is_safe_to_call_twice():
    assert launcher.shutdown_engines() == 0
    assert launcher.shutdown_engines() == 0


# ----------------------------------------------------------------- the preflight

def test_preflight_reports_problems_as_plain_sentences():
    problems = launcher.preflight()
    assert isinstance(problems, list)
    for p in problems:
        assert p and p[0].isupper(), "a message shown in a window should read as prose"


# -------------------------------------------------------------------- the bundle

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS bundle")
def test_bundle_is_well_formed(tmp_path):
    app = build_app.build(dest_dir=tmp_path)
    assert app.is_dir() and app.suffix == ".app"

    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    exe = app / "Contents/MacOS" / info["CFBundleExecutable"]
    assert exe.is_file(), "CFBundleExecutable must actually exist"
    assert exe.stat().st_mode & 0o111, "the launcher must be executable"
    assert info["CFBundleName"] == build_app.APP_NAME
    assert info["CFBundleIdentifier"] == build_app.BUNDLE_ID
    assert info["CFBundlePackageType"] == "APPL"
    # A Dock icon and a menu bar are the whole point - not a background agent.
    assert info["LSUIElement"] is False
    assert info["NSHighResolutionCapable"] is True

    stub = exe.read_text()
    assert "coach.desktop.launcher" in stub
    assert "/opt/homebrew/bin" in stub, "Homebrew stockfish must be on PATH"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS bundle")
def test_rebuilding_replaces_the_old_bundle(tmp_path):
    first = build_app.build(dest_dir=tmp_path)
    (first / "Contents" / "stale.txt").write_text("left over")
    second = build_app.build(dest_dir=tmp_path)
    assert first == second
    assert not (second / "Contents" / "stale.txt").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS bundle")
def test_the_stub_points_at_the_repo_it_was_built_from(tmp_path):
    repo = tmp_path / "somewhere" / "Chess"
    repo.mkdir(parents=True)
    app = build_app.build(dest_dir=tmp_path, repo=repo)
    stub = (app / "Contents/MacOS/chess-coach").read_text()
    assert str(repo) in stub
    # ...and says so plainly if that path has gone away.
    assert "make app" in stub
