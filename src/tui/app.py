from __future__ import annotations

import functools

from rich.markup import escape as _re

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Footer, Header, Input, Label,
    ListItem, ListView, RichLog, TextArea,
)

from .tools import TOOLS, TOOL_BY_ID, ToolDef
from . import backend


def _result_label(filename: str, score: float | None) -> str:
    """Build a Rich-markup label line for one result item."""
    name = _re(filename)
    if score is None:
        return f" {name}"
    capped = min(score, 1.0)
    filled = round(capped * 5)
    bar = "█" * filled + "░" * (5 - filled)
    color = "green" if score >= 0.8 else ("yellow" if score >= 0.6 else "red")
    return f" {name}  [{color}]{bar} {score:.2f}[/{color}]"


# ---------------------------------------------------------------------------
# Tool Picker Modal
# ---------------------------------------------------------------------------

class ToolPickerScreen(ModalScreen):
    """Side-by-side tool list with live description pane."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ToolPickerScreen {
        align: center middle;
    }
    #picker-outer {
        width: 84%;
        height: 74%;
        border: thick $primary;
        background: $surface;
        layout: vertical;
    }
    #picker-header {
        height: 1;
        background: $primary;
        padding: 0 2;
        color: $background;
        text-style: bold;
    }
    #picker-body {
        layout: horizontal;
        height: 1fr;
    }
    #tool-list {
        width: 40%;
        border-right: solid $primary-darken-2;
    }
    #tool-list > ListItem {
        padding: 0 1;
    }
    #desc-panel {
        width: 60%;
        padding: 1 2;
        layout: vertical;
        overflow-y: auto;
    }
    #desc-name {
        text-style: bold;
        color: $accent;
        width: 1fr;
    }
    #desc-body {
        color: $text-muted;
        margin-top: 1;
        width: 1fr;
    }
    #desc-params {
        margin-top: 1;
        color: $text;
        width: 1fr;
    }
    #picker-footer-bar {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        content-align: center middle;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="picker-outer"):
            yield Label(" Select Tool", id="picker-header")
            with Horizontal(id="picker-body"):
                with ListView(id="tool-list"):
                    for tool in TOOLS:
                        yield ListItem(
                            Label(f" {tool.label}  [{tool.shortcut}]"),
                            id=f"tool-{tool.id}",
                        )
                with Vertical(id="desc-panel"):
                    yield Label("", id="desc-name")
                    yield Label("", id="desc-body")
                    yield Label("", id="desc-params")
            yield Label(
                "↑↓ navigate   Enter: select   Esc: cancel",
                id="picker-footer-bar",
            )

    def on_mount(self) -> None:
        lv = self.query_one("#tool-list", ListView)
        if TOOLS:
            lv.index = 0
            self._show_desc(TOOLS[0])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            tool_id = event.item.id.removeprefix("tool-")
            tool = TOOL_BY_ID.get(tool_id)
            if tool:
                self._show_desc(tool)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.id.removeprefix("tool-"))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _show_desc(self, tool: ToolDef) -> None:
        self.query_one("#desc-name",   Label).update(tool.label)
        self.query_one("#desc-body",   Label).update(tool.description)
        param_lines = "Parameters:\n" + "\n".join(
            f"  • {p.label}" + (f"\n    {p.placeholder}" if p.placeholder else "")
            for p in tool.params
        )
        self.query_one("#desc-params", Label).update(param_lines)


# ---------------------------------------------------------------------------
# File Explain Modal  (drill-down level 2)
# ---------------------------------------------------------------------------

class ExplainScreen(ModalScreen):
    """Chunk-level breakdown for a single file."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    ExplainScreen {
        align: center middle;
    }
    #explain-outer {
        width: 90%;
        height: 84%;
        border: thick $primary;
        background: $surface;
        layout: vertical;
    }
    #explain-header {
        height: 1;
        background: $primary;
        padding: 0 2;
        color: $background;
        text-style: bold;
        overflow: hidden;
    }
    #explain-body {
        layout: horizontal;
        height: 1fr;
    }
    #chunk-list {
        width: 34%;
        border-right: solid $primary-darken-2;
    }
    #chunk-list > ListItem {
        padding: 0 1;
    }
    #chunk-right {
        width: 66%;
        layout: vertical;
    }
    #chunk-summary-bar {
        height: auto;
        max-height: 5;
        padding: 0 1;
        background: $surface-darken-1;
        border-bottom: solid $primary-darken-2;
        color: $text-muted;
    }
    #chunk-content-log {
        height: 1fr;
        padding: 0 1;
    }
    #explain-footer-bar {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        content-align: center middle;
    }
    """

    def __init__(self, file_path: str) -> None:
        super().__init__()
        self.file_path = file_path
        self.chunks: list[dict] = []

    def compose(self) -> ComposeResult:
        with Container(id="explain-outer"):
            yield Label(f" {self.file_path}", id="explain-header")
            with Horizontal(id="explain-body"):
                with ListView(id="chunk-list"):
                    pass
                with Vertical(id="chunk-right"):
                    yield Label("", id="chunk-summary-bar")
                    yield RichLog(id="chunk-content-log", highlight=True, wrap=True)
            yield Label("↑↓ select chunk   Esc: close", id="explain-footer-bar")

    def on_mount(self) -> None:
        log = self.query_one("#chunk-content-log", RichLog)
        log.write("[dim]Loading chunks...[/dim]")
        self.run_worker(self._load, thread=True)

    def _load(self) -> None:
        chunks = backend.get_file_chunks(self.file_path)
        self.app.call_from_thread(self._populate, chunks)

    def _populate(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        lv = self.query_one("#chunk-list", ListView)
        log = self.query_one("#chunk-content-log", RichLog)
        log.clear()
        if not chunks:
            log.write("[yellow]No indexed chunks found for this file.[/yellow]")
            return
        for chunk in chunks:
            lv.append(ListItem(
                Label(f"[{chunk['tier']}] {chunk['scope'][:38]}"),
                id=f"ck-{chunk['id']}",
            ))
        lv.index = 0
        self._show_chunk(chunks[0])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is not None and idx < len(self.chunks):
            self._show_chunk(self.chunks[idx])

    def _show_chunk(self, chunk: dict) -> None:
        summary_lbl = self.query_one("#chunk-summary-bar", Label)
        log = self.query_one("#chunk-content-log", RichLog)
        log.clear()
        if chunk.get("summary"):
            summary_lbl.update(f"Summary: {chunk['summary']}")
        else:
            summary_lbl.update(f"{chunk['tier']}  •  {chunk['scope']}")
        log.write(chunk["text"])

    def action_dismiss_modal(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Main Search Screen
# ---------------------------------------------------------------------------

class SearchScreen(Screen):
    """Primary interface: tool bar → param inputs → results list + preview."""

    BINDINGS = [
        Binding("t",      "open_tools",    "Tools"),
        Binding("e",      "explain_file",  "Explain"),
        Binding("ctrl+r", "quick_reindex", "Reindex"),
        Binding("ctrl+s", "run_tool",      "Run",    show=False),
        Binding("q",      "app.quit",      "Quit"),
    ]

    DEFAULT_CSS = """
    SearchScreen {
        layout: vertical;
    }

    /* ── tool bar ──────────────────────────────────────────────── */
    #tool-bar {
        height: 3;
        layout: horizontal;
        padding: 0 2;
        background: $surface-darken-1;
        border-bottom: solid $primary-darken-2;
    }
    #tb-prefix {
        width: auto;
        color: $text-muted;
        content-align: left middle;
        height: 3;
    }
    #tb-name {
        width: auto;
        color: $accent;
        text-style: bold;
        margin-left: 1;
        content-align: left middle;
        height: 3;
    }
    #tb-hint {
        width: 1fr;
        content-align: right middle;
        color: $text-muted;
        height: 3;
    }

    /* ── param inputs ──────────────────────────────────────────── */
    #input-area {
        height: auto;
        max-height: 16;
        margin: 1 1 0 1;
        padding: 0 1;
        border: solid $primary-darken-2;
        layout: vertical;
    }
    #input-area Input {
        margin-bottom: 0;
    }
    #input-area TextArea {
        height: 7;
    }

    /* ── main split ────────────────────────────────────────────── */
    #main-content {
        layout: horizontal;
        height: 1fr;
        margin: 1;
    }

    /* results */
    #results-panel {
        width: 36%;
        layout: vertical;
        border: solid $primary-darken-2;
        margin-right: 1;
    }
    #results-header {
        height: 1;
        background: $surface-darken-1;
        padding: 0 1;
        color: $text-muted;
    }
    #results-list {
        height: 1fr;
    }
    #results-list > ListItem {
        padding: 0 1;
    }

    /* preview + tool info */
    #right-panel {
        width: 64%;
        layout: vertical;
    }
    #code-preview {
        height: 2fr;
        border: solid $primary-darken-2;
        padding: 0 1;
        margin-bottom: 1;
    }
    #tool-info-panel {
        height: 1fr;
        border: solid $primary-darken-2;
        padding: 1 2;
        background: $surface-darken-1;
        layout: vertical;
        overflow-y: auto;
    }
    #ti-name {
        color: $accent;
        text-style: bold;
        height: auto;
        width: 1fr;
    }
    #ti-desc {
        color: $text-muted;
        height: auto;
        margin-top: 1;
        width: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.selected_tool: ToolDef = TOOLS[0]
        self.results: list[dict] = []
        self.selected_file: str = ""
        self._busy: bool = False

    # ── layout ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)

        with Horizontal(id="tool-bar"):
            yield Label("Tool: ", id="tb-prefix")
            yield Label(self.selected_tool.label, id="tb-name")
            yield Label("[t] change tool   Ctrl+S run   Ctrl+R reindex", id="tb-hint")

        with Container(id="input-area"):
            yield from self._build_param_widgets(self.selected_tool)

        with Horizontal(id="main-content"):
            with Vertical(id="results-panel"):
                yield Label("Results", id="results-header")
                yield ListView(id="results-list")
            with Vertical(id="right-panel"):
                yield RichLog(id="code-preview", highlight=True, markup=True, wrap=True)
                with Vertical(id="tool-info-panel"):
                    yield Label(self.selected_tool.label,       id="ti-name")
                    yield Label(self.selected_tool.description, id="ti-desc")

        yield Footer()

    @staticmethod
    def _build_param_widgets(tool: ToolDef):
        for param in tool.params:
            if param.multiline:
                yield TextArea(id=f"param-{param.name}")
            else:
                yield Input(
                    placeholder=f"{param.label}  —  {param.placeholder}",
                    id=f"param-{param.name}",
                )

    # ── startup ─────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        log = self.query_one("#code-preview", RichLog)
        log.write("[dim]Loading index — please wait...[/dim]")
        self.run_worker(self._load_backend, thread=True)

    def _load_backend(self) -> None:
        try:
            backend._get_server()
            self.app.call_from_thread(self._on_ready)
        except Exception as exc:
            self.app.call_from_thread(self._on_load_error, str(exc))

    def _on_ready(self) -> None:
        log = self.query_one("#code-preview", RichLog)
        log.clear()
        log.write(
            "[green]Index ready.[/green]  "
            "Type a query and press [bold]Enter[/bold] or [bold]Ctrl+S[/bold]."
        )
        self._focus_first_param()

    def _on_load_error(self, err: str) -> None:
        log = self.query_one("#code-preview", RichLog)
        log.clear()
        log.write(f"[red bold]Index load failed:[/red bold]\n\n{err}")

    # ── tool selection ───────────────────────────────────────────────────

    def action_open_tools(self) -> None:
        def _picked(tool_id: str | None) -> None:
            if tool_id and tool_id in TOOL_BY_ID:
                self.selected_tool = TOOL_BY_ID[tool_id]
                self.query_one("#tb-name",  Label).update(self.selected_tool.label)
                self.query_one("#ti-name",  Label).update(self.selected_tool.label)
                self.query_one("#ti-desc",  Label).update(self.selected_tool.description)
                self.call_after_refresh(self._rebuild_inputs_async)

        self.app.push_screen(ToolPickerScreen(), _picked)

    async def _rebuild_inputs_async(self) -> None:
        area = self.query_one("#input-area")
        await area.remove_children()
        for widget in self._build_param_widgets(self.selected_tool):
            await area.mount(widget)
        self._focus_first_param()

    def _focus_first_param(self) -> None:
        if self.selected_tool.params:
            try:
                w = self.query_one(f"#param-{self.selected_tool.params[0].name}")
                self.set_focus(w)
            except Exception:
                pass

    # ── running tools ────────────────────────────────────────────────────

    def _collect_params(self) -> dict:
        params: dict = {}
        for p in self.selected_tool.params:
            try:
                w = self.query_one(f"#param-{p.name}")
                val: str = w.text if isinstance(w, TextArea) else w.value
                if p.name == "deep":
                    params[p.name] = val.strip().lower() in ("y", "yes", "true", "1")
                elif p.name == "changed_files_only":
                    params[p.name] = val.strip().lower() not in ("full", "f", "false", "0")
                else:
                    params[p.name] = val
            except Exception:
                params[p.name] = (
                    False if p.name in ("deep", "changed_files_only") else ""
                )
        return params

    def action_run_tool(self) -> None:
        if self._busy:
            return
        params = self._collect_params()
        if self.selected_tool.params:
            first_key = self.selected_tool.params[0].name
            if not str(params.get(first_key, "")).strip():
                return
        self._start_tool(self.selected_tool.id, params)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_run_tool()

    def _start_tool(self, tool_id: str, params: dict) -> None:
        self._busy = True
        log = self.query_one("#code-preview", RichLog)
        log.clear()
        log.write(f"[dim]Running {self.selected_tool.label}...[/dim]")
        self.run_worker(
            functools.partial(self._tool_worker, tool_id, params),
            thread=True,
            exclusive=True,
        )

    def _tool_worker(self, tool_id: str, params: dict) -> None:
        try:
            output = backend.call_tool(tool_id, params)
            self.app.call_from_thread(self._on_done, output)
        except Exception as exc:
            self.app.call_from_thread(self._on_error, str(exc))

    def _on_done(self, output: str) -> None:
        self._busy = False
        log = self.query_one("#code-preview", RichLog)
        log.clear()
        log.write(output)

        parsed = backend.extract_results(output)
        lv = self.query_one("#results-list", ListView)
        lv.clear()
        self.results = []

        for item in parsed:
            fp = item["file"]
            score = item["score"]
            filename = fp.replace("\\", "/").split("/")[-1]
            self.results.append({"file": fp, "output": output, "score": score})
            lv.append(ListItem(Label(_result_label(filename, score))))

        n = len(parsed)
        self.query_one("#results-header", Label).update(
            f"Results ({n})" if n else "Results"
        )

    def _on_error(self, err: str) -> None:
        self._busy = False
        log = self.query_one("#code-preview", RichLog)
        log.clear()
        log.write(f"[red bold]Error:[/red bold] {err}")

    # ── results interaction ──────────────────────────────────────────────

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "results-list":
            return
        idx = event.list_view.index
        if idx is not None and idx < len(self.results):
            self.selected_file = self.results[idx]["file"]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "results-list":
            return
        idx = event.list_view.index
        if idx is not None and idx < len(self.results):
            fp = self.results[idx]["file"]
            self.selected_file = fp
            self.run_worker(
                functools.partial(self._load_file_chunks, fp),
                thread=True,
            )

    def _load_file_chunks(self, fp: str) -> None:
        chunks = backend.get_file_chunks(fp)
        self.app.call_from_thread(self._show_file_chunks, fp, chunks)

    def _show_file_chunks(self, fp: str, chunks: list[dict]) -> None:
        log = self.query_one("#code-preview", RichLog)
        log.clear()
        if not chunks:
            log.write(f"[yellow]No indexed chunks for:[/yellow] {fp}")
            return
        short = fp.replace("\\", "/").split("/")[-1]
        log.write(f"[bold]{short}[/bold]  [dim]{fp}[/dim]  ({len(chunks)} chunks)")
        log.write("─" * 60)
        for chunk in chunks[:6]:
            log.write(
                f"\n[bold cyan]{chunk['tier']}[/bold cyan]"
                f"  [dim]{chunk['scope']}[/dim]"
            )
            if chunk.get("summary"):
                log.write(f"[italic dim]{chunk['summary']}[/italic dim]")
            log.write(chunk["text"][:500])
            log.write("─" * 40)

    # ── other actions ────────────────────────────────────────────────────

    def action_explain_file(self) -> None:
        if self.selected_file:
            self.app.push_screen(ExplainScreen(self.selected_file))

    def action_quick_reindex(self) -> None:
        if not self._busy:
            self._start_tool("reindex", {"changed_files_only": True})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class IndexerApp(App):
    TITLE = "Code Intelligence"
    SUB_TITLE = "FAISS + SQLite"

    # Override Textual's built-in ctrl+q → quit so VS Code doesn't get a
    # spurious "view picker" popup when the TUI is running in its terminal.
    # Users quit with [q] in the search screen instead.
    BINDINGS = [Binding("ctrl+q", "noop", show=False)]

    DEFAULT_CSS = """
    Header { background: $primary; color: $background; }
    """

    def action_noop(self) -> None:
        pass

    def on_mount(self) -> None:
        self.push_screen(SearchScreen())
