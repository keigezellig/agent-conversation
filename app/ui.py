"""A desktop window to set up conversations in, and to watch them as they happen.

Each conversation is set up in a form: its subject, its cast, how many turns, and whether the
personas may interrupt. The model and the voices are not in it; they stay as the command line, or
.env, set them for the whole run, and whatever else the command line said fills the first form in.

The runner hands every line it produces to a `show` callback, which is all this needs: the lines
arrive on a worker thread, go onto a queue, and the Tk event loop drains them. A line that starts
with a persona's "NAME (MOOD): " label is that persona's reply; anything else is a note the runner
made about the run itself.

With read-aloud, a reply is shown at the moment it starts being spoken, so the persona lit up in the
cast is the one you are hearing.
"""

import argparse
import asyncio
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont, ttk

from .agents import (NOT_SPOKEN, _personas, attribute, conversation_args, parse_args,
                     run_conversation)
from .speech import DEFAULT_VOICES, assign_voices
from .turns import DASH, EAGERNESS_SCALE

POLL_MS = 60

# A dark window, because a conversation is mostly text and this is what replaces a terminal.
BG = "#17161c"
PANEL = "#1e1d25"
PANEL_LIT = "#2a2833"
LINE = "#32303c"
INK = "#eceaf2"
INK_2 = "#a7a2b3"
INK_3 = "#736e80"
CUT = "#fb923c"
LIVE = "#4ade80"
# One per persona, in order; they repeat if a cast is larger than the palette.
VOICES_OF_THE_CAST = ("#7aa2f7", "#e0a458", "#f4707f", "#bb9af7", "#5ccfb4", "#e8c547", "#9ad17e")


SANS = ["Inter", "DejaVu Sans", "Segoe UI", "Helvetica"]
MONO = ["JetBrains Mono", "DejaVu Sans Mono", "Consolas", "Courier"]
FIELD = dict(bg=PANEL, fg=INK, insertbackground=INK, relief="flat", highlightthickness=1,
             highlightbackground=LINE, highlightcolor=INK_3)
FLAT = dict(bg=BG, activebackground=PANEL, relief="flat", bd=0, highlightthickness=0, cursor="hand2")


def _font(root, families, size, weight="normal", slant="roman"):
    available = {family.lower() for family in tkfont.families(root)}
    family = next((f for f in families if f.lower() in available), families[-1])
    return tkfont.Font(root=root, family=family, size=size, weight=weight, slant=slant)


class Setup(tk.Frame):
    """The form a conversation is set up in: what the command line would otherwise say about it.

    It starts from `start_from`, the command line or the conversation held last, and hands the
    settings to `on_start`. `busy` says whether a conversation is still running, in which case the
    form can be filled in but not started, so two conversations never speak over each other.
    """

    def __init__(self, notebook, base: argparse.Namespace, start_from: argparse.Namespace,
                 on_start, busy):
        super().__init__(notebook, bg=BG, padx=24, pady=18)
        self.base = base
        self.on_start = on_start
        self.busy = busy
        self.cast = []
        self._build(start_from)
        self.refresh()

    def _build(self, args: argparse.Namespace) -> None:
        root = self.winfo_toplevel()
        self.f_field = _font(root, SANS, 11)
        self.f_small = _font(root, SANS, 9)
        self.f_mono = _font(root, MONO, 9)

        tk.Label(self, text="NEW CONVERSATION", bg=BG, fg=INK_3, font=self.f_mono, anchor="w").pack(fill="x")

        self._caption("Subject")
        self.subject = tk.Text(self, height=3, wrap="word", font=self.f_field, padx=8, pady=6, **FIELD)
        self.subject.insert("1.0", args.subject or "")
        self.subject.bind("<Tab>", lambda _: (self.subject.tk_focusNext().focus(), "break")[1])
        self.subject.pack(fill="x")

        self._caption("Cast")
        self.table = tk.Frame(self, bg=BG)
        self.table.pack(fill="x")
        self.table.columnconfigure(1, weight=1)
        for column, heading in enumerate(("name", "mood", "voice  (empty: one from the pool)")):
            tk.Label(self.table, text=heading, bg=BG, fg=INK_3, font=self.f_small,
                     anchor="w").grid(row=0, column=column, sticky="w", padx=(0, 8))
        rows = self._rows(args.agent_specs)
        for name, mood, voice in rows + [("", "", "")] * (2 - len(rows)):
            self.add(name, mood, voice)
        tk.Button(self, text="+ add agent", command=lambda: self.add(focus=True), fg=INK_2,
                  activeforeground=INK, font=self.f_small, **FLAT).pack(anchor="w", pady=(4, 0))

        self._caption("Run")
        options = tk.Frame(self, bg=BG)
        options.pack(fill="x")
        tk.Label(options, text="turns", bg=BG, fg=INK_2, font=self.f_small).pack(side="left")
        self.turns = tk.StringVar(value=str(args.turns))
        tk.Spinbox(options, from_=1, to=1000, width=5, textvariable=self.turns, font=self.f_field,
                   buttonbackground=PANEL, **FIELD).pack(side="left", padx=(6, 20))
        tk.Label(options, text="seed", bg=BG, fg=INK_2, font=self.f_small).pack(side="left")
        self.seed = tk.StringVar(value="" if args.seed is None else str(args.seed))
        tk.Entry(options, textvariable=self.seed, width=8, font=self.f_field,
                 **FIELD).pack(side="left", padx=(6, 20))
        self.interruptions = tk.BooleanVar(value=bool(args.interruptions))
        tk.Checkbutton(options, text="the agents may interrupt each other", variable=self.interruptions,
                       bg=BG, fg=INK_2, selectcolor=PANEL, activebackground=BG, activeforeground=INK,
                       highlightthickness=0, bd=0, font=self.f_small).pack(side="left")

        tk.Label(self, text=f"model {self.base.model} on the {self.base.llm_api} API  ·  read-aloud "
                            f"{'on' if self.base.read_aloud else 'off'}  ·  both set on the command line",
                 bg=BG, fg=INK_3, font=self.f_mono, anchor="w").pack(fill="x", pady=(18, 0))

        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", pady=(12, 0))
        self.go = ttk.Button(bottom, text="Start conversation", style="Action.TButton", command=self.start)
        self.go.pack(side="right")
        self.problem = tk.Label(bottom, text="", bg=BG, fg=CUT, font=self.f_mono, anchor="w")
        self.problem.pack(side="left", fill="x", expand=True)

    def _caption(self, text: str) -> None:
        tk.Label(self, text=text, bg=BG, fg=INK, font=_font(self.winfo_toplevel(), SANS, 10, "bold"),
                 anchor="w").pack(fill="x", pady=(14, 4))

    @staticmethod
    def _rows(specs) -> list[tuple[str, str, str]]:
        """The command line's NAME|MOOD|VOICE specs as rows, taken as they are: the form checks them."""
        return [tuple((*(part.strip() for part in spec.split("|", 2)), "", "")[:3]) for spec in specs or []]

    def add(self, name: str = "", mood: str = "", voice: str = "", focus: bool = False) -> None:
        row = {}
        for key, value, width in (("name", name, 14), ("mood", mood, 40), ("voice", voice, 26)):
            row[key] = tk.Entry(self.table, width=width, font=self.f_field, **FIELD)
            row[key].insert(0, value)
        row["remove"] = tk.Button(self.table, text="✕", fg=INK_3, activeforeground=CUT,
                                  font=self.f_small, command=lambda: self._remove(row), **FLAT)
        self.cast.append(row)
        self._regrid()
        if focus:
            row["name"].focus_set()

    def _remove(self, row: dict) -> None:
        self.cast.remove(row)
        for widget in row.values():
            widget.destroy()
        self._regrid()

    def _regrid(self) -> None:
        for line, row in enumerate(self.cast, start=1):
            for column, key in enumerate(("name", "mood", "voice", "remove")):
                row[key].grid(row=line, column=column, sticky="ew", padx=(0, 8), pady=2, ipady=3)

    def refresh(self) -> None:
        busy = self.busy()
        self.go.configure(state="disabled" if busy else "normal")
        self.problem.configure(text="starts once the running conversation has closed" if busy else "",
                               fg=INK_3)

    def start(self) -> None:
        if self.busy():
            return
        try:
            args = conversation_args(
                self.base, self.subject.get("1.0", "end"),
                [(row["name"].get(), row["mood"].get(), row["voice"].get()) for row in self.cast],
                self.turns.get(), self.seed.get(), self.interruptions.get())
        except ValueError as error:
            self.problem.configure(text=str(error), fg=CUT)
            return
        self.on_start(args)


class Conversation(ttk.Frame):
    """One conversation: its cast down the side, its replies as they arrive, and what it is doing."""

    def __init__(self, notebook, args: argparse.Namespace, on_finished=None):
        super().__init__(notebook)
        self.args = args
        self.on_finished = on_finished
        self.personas = _personas(args)
        self.colours = {name: VOICES_OF_THE_CAST[i % len(VOICES_OF_THE_CAST)]
                        for i, (name, _, _) in enumerate(self.personas)}
        self.voices = assign_voices(self.personas, [v.strip() for v in (args.tts_voices or DEFAULT_VOICES).split(",") if v.strip()])
        self.inbox = queue.Queue()
        self.turn = 0
        self.rows = {}
        self.was_cut = False  # the previous reply broke off, so this one is the persona cutting in
        self.over = False  # closed, failed or stopped: the window has said so and moved on
        self.stopped = False
        self.loop = self.task = None
        self._build()
        self._pump()

    # ---------------------------------------------------------------- widgets
    def _build(self) -> None:
        root = self.winfo_toplevel()
        self.f_name = _font(root, SANS, 11, "bold")
        self.f_body = _font(root, ["Georgia", "DejaVu Serif", "Times New Roman", "Times"], 12)
        self.f_small = _font(root, SANS, 9)
        self.f_mono = _font(root, MONO, 9)

        header = tk.Frame(self, bg=BG, padx=16, pady=12)
        header.pack(fill="x")
        self.stop_button = ttk.Button(header, text="Stop", style="Stop.TButton", command=self.stop)
        self.stop_button.pack(side="right", anchor="n", padx=(16, 0))
        self.subject = tk.Label(header, text=self.args.subject, bg=BG, fg=INK, anchor="w",
                                font=_font(root, SANS, 14, "bold"),
                                wraplength=900, justify="left")
        self.subject.pack(fill="x")
        self.meta = tk.Label(header, text="starting…", bg=BG, fg=INK_3, anchor="w", font=self.f_mono)
        self.meta.pack(fill="x", pady=(4, 0))

        self.status = tk.Label(self, text="", bg=PANEL, fg=INK_2, anchor="w", padx=16, pady=7,
                               font=self.f_mono)
        self.status.pack(side="bottom", fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        cast = tk.Frame(body, bg=PANEL, width=250, padx=12, pady=12)
        cast.pack(side="left", fill="y")
        cast.pack_propagate(False)
        tk.Label(cast, text="CAST", bg=PANEL, fg=INK_3, anchor="w", font=self.f_mono).pack(fill="x", pady=(0, 8))
        for name, mood, _ in self.personas:
            self.rows[name] = self._cast_row(cast, name, mood)

        wrap = tk.Frame(body, bg=BG)
        wrap.pack(side="left", fill="both", expand=True)
        self.text = tk.Text(wrap, bg=BG, fg=INK, bd=0, highlightthickness=0, wrap="word",
                            padx=20, pady=16, spacing1=2, spacing3=4, cursor="arrow",
                            font=self.f_body, insertbackground=BG)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self._tags()
        self.text.configure(state="disabled")

    def _cast_row(self, parent, name: str, mood: str) -> dict:
        colour = self.colours[name]
        row = tk.Frame(parent, bg=PANEL, padx=8, pady=7)
        row.pack(fill="x", pady=(0, 5))
        top = tk.Frame(row, bg=PANEL)
        top.pack(fill="x")
        marker = tk.Label(top, text=" ", bg=PANEL, fg=colour, font=self.f_small, width=2)
        marker.pack(side="left")
        tk.Label(top, text=name, bg=PANEL, fg=colour, font=self.f_name, anchor="w").pack(side="left")
        said = tk.Label(top, text="", bg=PANEL, fg=INK_3, font=self.f_mono)
        said.pack(side="right")
        mood_label = tk.Label(row, text=mood, bg=PANEL, fg=INK_3, font=self.f_small, anchor="w",
                              wraplength=200, justify="left")
        mood_label.pack(fill="x", pady=(2, 0))
        detail = tk.Label(row, text="", bg=PANEL, fg=INK_3, font=self.f_mono, anchor="w",
                          wraplength=200, justify="left")
        detail.pack(fill="x", pady=(3, 0))
        return {"frame": row, "top": top, "marker": marker, "mood": mood_label,
                "detail": detail, "said": said, "count": 0}

    def _tags(self) -> None:
        for name, colour in self.colours.items():
            self.text.tag_configure(f"who:{name}", foreground=colour, font=self.f_name,
                                    spacing1=12, lmargin1=0)
        self.text.tag_configure("mood", foreground=INK_3, font=self.f_small, spacing1=12)
        self.text.tag_configure("body", foreground=INK, font=self.f_body, lmargin1=0, lmargin2=0,
                                spacing3=10)
        self.text.tag_configure("cut", foreground=CUT, font=self.f_name)
        self.text.tag_configure("note", foreground=INK_3, font=self.f_mono, spacing1=4, spacing3=8)
        self.text.tag_configure("problem", foreground=CUT, font=self.f_mono, spacing1=0, spacing3=10)

    # ------------------------------------------------------------- the run
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            asyncio.run(self._hold())
            self.inbox.put(("done", None))
        except BaseException as error:  # a failed run belongs in the window, not in a traceback
            if not self.stopped:
                self.inbox.put(("failed", str(error) or type(error).__name__))

    async def _hold(self) -> None:
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        if self.stopped:  # stopped before there was a task to cancel
            return
        await run_conversation(self.args,
                               show=lambda line: self.inbox.put(("line", line)),
                               on_start=lambda info: self.inbox.put(("start", info)))

    def stop(self) -> None:
        """Stop the conversation where it is, the reply being spoken included.

        The window moves on at once. The run itself winds down in its thread, finishing any request
        to the model it is waiting on; what it says after this is not shown.
        """
        if self.over:
            return
        self.stopped = True
        if self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self._note(f"stopped after {self.turn} of {self.args.turns} turns", tag="problem")
        self._end(f"stopped after {self.turn} of {self.args.turns} turns", CUT)

    def _pump(self) -> None:
        while True:
            try:
                kind, value = self.inbox.get_nowait()
            except queue.Empty:
                break
            if not self.stopped:
                getattr(self, f"_on_{kind}")(value)
        self.after(POLL_MS, self._pump)

    def _on_start(self, info: dict) -> None:
        self.meta.configure(text=f"{info['id']}  ·  {info['model']}  ·  {info['turns']} turns"
                                 + ("  ·  read-aloud" if info["read_aloud"] else "")
                                 + ("  ·  interruptions" if info["eagerness"] else ""))
        for name, _, _ in self.personas:
            bits = []
            if rating := (info["eagerness"] or {}).get(name):
                bits.append("●" * rating + "○" * (EAGERNESS_SCALE - rating) + f"  eager {rating}/{EAGERNESS_SCALE}")
            if language := (info["languages"] or {}).get(name):
                bits.append(f"writes {language}")
            if info["read_aloud"]:
                bits.append(self.voices.get(name, ""))
            self.rows[name]["detail"].configure(text="\n".join(bit for bit in bits if bit))

    def _on_line(self, line: str) -> None:
        speaker, said = attribute(line, self.personas)
        if speaker is None:
            self._note(line)
            return
        self.turn += 1
        cut = self.args.interruptions and said.endswith(DASH)
        self._reply(speaker, said, cut_in=self.was_cut, cut_off=cut)
        self.was_cut = cut
        self._lit(speaker)
        doing = "is speaking" if self.args.read_aloud else "replied"
        self.status.configure(text=f"turn {self.turn} of {self.args.turns}  ·  {speaker} {doing}",
                              fg=self.colours[speaker])

    def _on_done(self, _) -> None:
        self._end(f"conversation closed after {self.turn} turns", LIVE)

    def _on_failed(self, reason: str) -> None:
        self._note(f"the conversation stopped: {reason}", tag="problem")
        self._end(f"stopped: {reason}", CUT)

    def _end(self, status: str, colour: str) -> None:
        self.over = True
        self._lit(None)
        self.status.configure(text=status, fg=colour)
        self.stop_button.pack_forget()
        if self.on_finished:
            self.on_finished()

    # ------------------------------------------------------------- drawing
    def _write(self, chunks) -> None:
        self.text.configure(state="normal")
        for chunk, tag in chunks:
            self.text.insert("end", chunk, tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _reply(self, speaker: str, said: str, cut_in: bool, cut_off: bool) -> None:
        mood = next(mood for name, mood, _ in self.personas if name == speaker)
        marks = []
        if cut_in:
            marks.append("cuts in")
        if cut_off:
            marks.append("cut off")
        body = said[:-len(DASH)] if cut_off else said
        self._write([
            (f"{speaker}  ", f"who:{speaker}"),
            (f"{mood}" + (f"   ·   {' · '.join(marks)}" if marks else "") + "\n", "mood"),
            (body, "body"),
            *([(DASH, "cut")] if cut_off else []),
            ("\n", "body"),
        ])

    def _note(self, line: str, tag: str = "note") -> None:
        trouble = line.startswith(NOT_SPOKEN.split("{", 1)[0])
        self._write([(line + "\n", "problem" if trouble else tag)])

    def _lit(self, speaker: str | None) -> None:
        for name, row in self.rows.items():
            on = name == speaker
            if on:
                row["count"] += 1
            background = PANEL_LIT if on else PANEL
            for widget in (row["frame"], row["top"], row["marker"], row["mood"], row["detail"], row["said"]):
                widget.configure(bg=background)
            row["marker"].configure(text="▶" if on else " ")
            row["said"].configure(text=f"{row['count']}" if row["count"] else "")


class Window:
    """The window itself: a tab per conversation, and a form to set up the next one in."""

    def __init__(self, args: argparse.Namespace):
        self.base = args
        self.last = args  # what the next form starts from: the command line, then the last one held
        self.held = 0
        self.running = None  # the conversation still going, if any
        self.setup = None
        self.root = tk.Tk()
        self.root.title("agent-bridge — conversations")
        self.root.geometry("1180x760")
        self.root.minsize(820, 520)
        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(10, 8, 10, 0))
        style.configure("TNotebook.Tab", background=PANEL, foreground=INK_2, padding=(14, 6),
                        borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL_LIT)], foreground=[("selected", INK)])
        style.configure("TFrame", background=BG)
        style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                        arrowcolor=INK_3, borderwidth=0)
        style.configure("Action.TButton", background=PANEL_LIT, foreground=INK, borderwidth=0,
                        padding=(12, 5))
        style.map("Action.TButton", foreground=[("disabled", INK_3)])
        style.configure("Stop.TButton", background=PANEL_LIT, foreground=CUT, borderwidth=0,
                        padding=(14, 5))
        style.map("Stop.TButton", background=[("active", LINE)])

        footer = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        footer.pack(side="bottom", fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 0))
        ttk.Button(footer, text="New conversation", style="Action.TButton",
                   command=self.new).pack(side="right")
        tk.Label(footer, text="one conversation runs at a time, so the voices never overlap  ·  "
                              "ctrl+n new  ·  ctrl+enter start  ·  esc stop",
                 bg=BG, fg=INK_3, font=("TkFixedFont", 8)).pack(side="left")

        self.root.bind("<Control-w>", lambda _: self.root.destroy())
        self.root.bind("<Control-n>", lambda _: self.new())
        self.root.bind("<Control-Return>", lambda _: self.setup and self.setup.start())
        self.root.bind("<Escape>", lambda _: self.running is not None and self.running.stop())

    def new(self) -> None:
        if self.setup is None:
            self.setup = Setup(self.tabs, self.base, self.last, on_start=self.hold,
                               busy=lambda: self.running is not None)
            self.tabs.add(self.setup, text="New conversation")
        self.tabs.select(self.setup)

    def hold(self, args: argparse.Namespace) -> None:
        if self.running is not None:
            return
        self.tabs.forget(self.setup)
        self.setup.destroy()
        self.setup = None
        self.held += 1
        self.last = args
        view = self.running = Conversation(self.tabs, args, on_finished=self._finished)
        self.tabs.add(view, text=f"Conversation {self.held}")
        self.tabs.select(view)
        view.start()

    def _finished(self) -> None:
        self.running = None
        if self.setup is not None:
            self.setup.refresh()

    def run(self) -> None:
        self.new()
        self.root.mainloop()


def main() -> None:
    Window(parse_args(need_conversation=False)).run()


if __name__ == "__main__":
    main()
