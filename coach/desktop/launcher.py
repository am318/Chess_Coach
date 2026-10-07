"""Desktop entry point: run the coach as a normal Mac application.

The server runs in a background thread inside this process rather than as a
child, so quitting the app genuinely stops it - the failure mode with a shell
wrapper is an orphaned uvicorn holding port 8000 until you go hunting for it in
Activity Monitor.

A small Tk window is what makes it an application rather than a script: it gives
the bundle a Dock icon, a menu bar and a working Cmd-Q, and it is somewhere to
put a real error message when Stockfish or ``coach.toml`` is missing - a
double-clicked script that dies has nowhere to say why.
"""

from __future__ import annotations

import queue
import socket
import threading
import webbrowser

from coach.config import CONFIG_PATH, ROOT

DEFAULT_PORT = 8000
HOST = "0.0.0.0"


def shutdown_engines() -> int:
    """Close every Stockfish this process opened.

    They are child *processes*: the analysis workers, the sparring bot and the
    drill adjudicator each hold one, and none of them go away just because the
    interpreter exits. Without this, quitting leaves orphans behind.
    """
    try:
        from coach.sparring.backend import close_all_engines

        return close_all_engines()
    except Exception:  # noqa: BLE001 - shutting down regardless
        return 0


def find_port(preferred: int = DEFAULT_PORT) -> int:
    """The preferred port if it's free, otherwise any free one."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    return preferred


def local_ip() -> str | None:
    """LAN address, so the window can show a URL that works from a phone."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("192.168.1.1", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def preflight() -> list[str]:
    """Problems worth telling the user about before anything else happens."""
    import shutil

    problems = []
    if not CONFIG_PATH.is_file():
        problems.append(
            "No coach.toml yet — copy coach.example.toml to coach.toml and add "
            "your Lichess / Chess.com usernames."
        )
    try:
        from coach.config import CoachConfig

        path = CoachConfig.load().engine.stockfish_path
    except Exception:  # noqa: BLE001
        path = "stockfish"
    if not (shutil.which(path) or shutil.which("stockfish")):
        problems.append(
            "Stockfish not found — run `brew install stockfish`. Sparring and "
            "analysis need it."
        )
    return problems


class ServerThread:
    """uvicorn on a background thread, with a shutdown that actually shuts down."""

    def __init__(self, port: int, host: str = HOST) -> None:
        self.port = port
        self.host = host
        self._server = None
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self, ready: queue.Queue) -> None:
        import uvicorn

        from coach.server.app import app

        config = uvicorn.Config(app, host=self.host, port=self.port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)

        def run() -> None:
            try:
                self._server.run()
            except BaseException as exc:  # noqa: BLE001 - surfaced in the window
                self.error = exc
            finally:
                ready.put("stopped")

        self._thread = threading.Thread(target=run, daemon=True, name="coach-server")
        self._thread.start()

        # Poll until it's actually accepting connections before opening a browser.
        for _ in range(100):
            if self.error is not None:
                ready.put("error")
                return
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    ready.put("up")
                    return
            threading.Event().wait(0.1)
        ready.put("timeout")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._server.force_exit = True
        if self._thread is not None:
            self._thread.join(timeout=4)
        shutdown_engines()


def run_gui() -> int:
    import tkinter as tk
    from tkinter import font as tkfont

    port = find_port()
    url = f"http://localhost:{port}/"
    lan = local_ip()
    server = ServerThread(port)
    ready: queue.Queue = queue.Queue()

    root = tk.Tk()
    root.title("Chess Coach")
    root.configure(bg="#1b1a17")
    root.geometry("380x230")
    root.resizable(False, False)

    title_font = tkfont.Font(family="Helvetica Neue", size=17, weight="bold")
    body_font = tkfont.Font(family="Helvetica Neue", size=12)
    mono_font = tkfont.Font(family="Menlo", size=11)

    tk.Label(root, text="Chess Coach", font=title_font, bg="#1b1a17",
             fg="#e8e4dc").pack(pady=(20, 2))
    status = tk.Label(root, text="Starting…", font=body_font, bg="#1b1a17", fg="#9a938a")
    status.pack()
    addr = tk.Label(root, text="", font=mono_font, bg="#1b1a17", fg="#7ab8ff")
    addr.pack(pady=(8, 0))

    buttons = tk.Frame(root, bg="#1b1a17")
    buttons.pack(pady=16)
    open_btn = tk.Button(buttons, text="Open in browser", state="disabled",
                         command=lambda: webbrowser.open(url))
    open_btn.pack(side="left", padx=5)

    def quit_app() -> None:
        status.config(text="Stopping…")
        root.update_idletasks()
        server.stop()
        root.destroy()

    tk.Button(buttons, text="Quit", command=quit_app).pack(side="left", padx=5)

    problems = preflight()
    if problems:
        tk.Label(root, text="\n".join(problems), font=body_font, bg="#1b1a17",
                 fg="#e8b96b", wraplength=340, justify="left").pack(padx=18)

    def on_ready() -> None:
        try:
            state = ready.get_nowait()
        except queue.Empty:
            root.after(120, on_ready)
            return
        if state == "up":
            status.config(text="Running — closing this window quits the app.")
            addr.config(text=url + (f"\n{lan}:{port} (from your phone)" if lan else ""))
            open_btn.config(state="normal")
            webbrowser.open(url)
        else:
            detail = f"{type(server.error).__name__}: {server.error}" if server.error else state
            status.config(text=f"Could not start the server — {detail}", fg="#e88")

    threading.Thread(target=server.start, args=(ready,), daemon=True).start()
    root.after(120, on_ready)

    # Cmd-Q, the red close button and the Dock's Quit all land here.
    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.createcommand("::tk::mac::Quit", quit_app)
    root.lift()
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0


def main() -> int:
    import atexit
    import os
    import signal

    # A double-clicked bundle starts in "/", so make the repo the working
    # directory - coach.toml and coach.db are resolved relative to it.
    os.chdir(ROOT)

    # Cover the exits Tk never sees: the Dock's Quit, `killall`, a logout. Only a
    # force-quit (SIGKILL) can still orphan an engine, and nothing can help there.
    atexit.register(shutdown_engines)

    def _terminate(_signum, _frame):
        shutdown_engines()
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _terminate)
        except (ValueError, OSError):
            pass

    try:
        return run_gui()
    except Exception as exc:  # noqa: BLE001 - last resort, report via a dialog
        import subprocess

        subprocess.run([
            "osascript", "-e",
            f'display alert "Chess Coach could not start" message "{exc}"',
        ], check=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
