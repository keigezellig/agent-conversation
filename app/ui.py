"""A desktop window that shows runner conversations as they happen, and speaks them aloud.

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

from .agents import NOT_SPOKEN, _personas, attribute, parse_args, run_conversation
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


def _font(root, families, size, weight="normal", slant="roman"):
    available = {family.lower() for family in tkfont.families(root)}
    family = next((f for f in families if f.lower() in available), families[-1])
    return tkfont.Font(root=root, family=family, size=size, weight=weight, slant=slant)


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
        self._build()
        self._pump()

    # ---------------------------------------------------------------- widgets
    def _build(self) -> None:
        root = self.winfo_toplevel()
        self.f_name = _font(root, ["Inter", "DejaVu Sans", "Segoe UI", "Helvetica"], 11, "bold")
        self.f_body = _font(root, ["Georgia", "DejaVu Serif", "Times New Roman", "Times"], 12)
        self.f_small = _font(root, ["Inter", "DejaVu Sans", "Segoe UI", "Helvetica"], 9)
        self.f_mono = _font(root, ["JetBrains Mono", "DejaVu Sans Mono", "Consolas", "Courier"], 9)

        header = tk.Frame(self, bg=BG, padx=16, pady=12)
        header.pack(fill="x")
        self.subject = tk.Label(header, text=self.args.subject, bg=BG, fg=INK, anchor="w",
                                font=_font(root, ["Inter", "DejaVu Sans", "Helvetica"], 14, "bold"),
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
            asyncio.run(run_conversation(self.args,
                                         show=lambda line: self.inbox.put(("line", line)),
                                         on_start=lambda info: self.inbox.put(("start", info))))
            self.inbox.put(("done", None))
        except BaseException as error:  # a failed run belongs in the window, not in a traceback
            self.inbox.put(("failed", str(error) or type(error).__name__))

    def _pump(self) -> None:
        while True:
            try:
                kind, value = self.inbox.get_nowait()
            except queue.Empty:
                break
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
        self._lit(None)
        self.status.configure(text=f"conversation closed after {self.turn} turns", fg=LIVE)
        if self.on_finished:
            self.on_finished()

    def _on_failed(self, reason: str) -> None:
        self._lit(None)
        self._note(f"the conversation stopped: {reason}", tag="problem")
        self.status.configure(text=f"stopped: {reason}", fg=CUT)
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
    """The window itself: a tab per conversation, and a way to hold another one with the same cast."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.held = 0
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
        style.configure("Again.TButton", background=PANEL_LIT, foreground=INK, borderwidth=0,
                        padding=(12, 5))

        footer = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        footer.pack(side="bottom", fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 0))
        self.again = ttk.Button(footer, text="Hold another conversation", style="Again.TButton",
                                command=self.hold, state="disabled")
        self.again.pack(side="right")
        tk.Label(footer, text="one conversation per tab · a new one starts only once this one is "
                              "closed, so the voices never overlap",
                 bg=BG, fg=INK_3, font=("TkFixedFont", 8)).pack(side="left")

        self.root.bind("<Control-w>", lambda _: self.root.destroy())
        self.root.bind("<Control-n>", lambda _: self.hold() if str(self.again["state"]) == "normal" else None)

    def hold(self) -> None:
        self.held += 1
        view = Conversation(self.tabs, self.args, on_finished=lambda: self.again.configure(state="normal"))
        self.tabs.add(view, text=f"Conversation {self.held}")
        self.tabs.select(view)
        self.again.configure(state="disabled")
        view.start()

    def run(self) -> None:
        self.hold()
        self.root.mainloop()


def main() -> None:
    Window(parse_args()).run()


if __name__ == "__main__":
    main()
