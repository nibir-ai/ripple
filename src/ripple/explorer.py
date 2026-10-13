"""Keyboard explorer over immutable evidence reports; no UI-specific analysis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Input, OptionList, RichLog, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker

from ripple import __version__
from ripple.errors import RippleError
from ripple.explorer_data import ExplorerSession, SourceExcerpt
from ripple.models import Change, EvidencePath, Impact, Location
from ripple.output import safe_text

PAGE_SIZE = 200
CHANGE_LIMIT = 500
FILTERS = ("All", "Modified", "Added", "Deleted", "Relocated")


def literal(value: str) -> Text:
    return Text("\n".join(safe_text(line) for line in value.split("\n")))


def path_text(paths: tuple[EvidencePath, ...]) -> str:
    lines: list[str] = []
    for path in paths:
        lines.extend((f"Change {path.origin_change_id} · {path.snapshot} · tree {path.tree}", ""))
        for step in path.steps:
            lines.append(f"{step.caller} --{step.edge_type}--> {step.callee}")
            lines.append(f"  {step.site.path}:{step.site.line}:{step.site.column}")
            lines.append(f"  {step.resolver_rule}: {step.explanation}")
        lines.append("")
    return "\n".join(lines)


class DetailLog(RichLog, can_focus=True):
    pass


class DetailScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close"), ("q", "close", "Close")]
    DEFAULT_CSS = """
    DetailScreen { align: center middle; background: #181825 85%; }
    DetailScreen > Vertical {
        width: 90%; height: 85%; border: round #b4befe; background: #1e1e2e;
    }
    DetailScreen Static { height: auto; padding: 1 2; }
    DetailScreen VerticalScroll { height: 1fr; }
    DetailScreen Button { width: 100%; }
    """

    def __init__(self, title: str, content: str):
        super().__init__()
        self.heading = title
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(literal(self.heading))
            with VerticalScroll():
                yield Static(literal(self.content))
            yield Button("Close · Escape", id="close-detail")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class FilterScreen(ModalScreen[str]):
    BINDINGS = [("escape", "close", "Cancel")]
    DEFAULT_CSS = """
    FilterScreen { align: center middle; background: #181825 85%; }
    FilterScreen > Vertical {
        width: 40; height: auto; border: round #b4befe; background: #1e1e2e;
    }
    FilterScreen Static { height: 3; padding: 1; }
    FilterScreen OptionList { height: 7; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Filter changes · Enter to choose", markup=False)
            yield OptionList(*(Option(literal(value), id=value) for value in FILTERS))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "All")

    def action_close(self) -> None:
        self.dismiss("")


@dataclass(frozen=True)
class Item:
    kind: str
    index: int


class ExplorerApp(App[int]):
    TITLE = "Ripple"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #181825; color: #cdd6f4; }
    #masthead {
        height: 2; padding: 0 1; background: #181825; color: #89b4fa; text-style: bold;
    }
    #summary { height: 1; padding: 0 1; color: #a6e3a1; }
    #status { height: 2; padding: 0 1; color: #f9e2af; }
    #search { display: none; height: 3; }
    #pane-label { height: 1; padding: 0 1; color: #a6adc8; }
    #panes { height: 1fr; }
    .pane {
        border: round #45475a; width: 1fr; height: 100%; background: #1e1e2e;
        scrollbar-background: #1e1e2e; scrollbar-color: #45475a;
        scrollbar-color-active: #b4befe;
    }
    .pane:focus-within { border: round #b4befe; border-title-color: #b4befe; }
    #changes { width: 29%; }
    #impacts { width: 34%; }
    #inspector { width: 37%; padding: 0 1; }
    DataTable { background: #1e1e2e; }
    DataTable > .datatable--header { background: #313244; color: #a6adc8; }
    DataTable > .datatable--cursor { background: #45475a; color: #cdd6f4; }
    DataTable > .datatable--odd-row { background: #1e1e2e; }
    DataTable > .datatable--even-row { background: #202030; }
    Tree { background: #1e1e2e; }
    Tree > .tree--cursor { background: #313244; color: #b4befe; }
    Tree > .tree--guides { color: #45475a; }
    .medium #changes { width: 42%; }
    .medium #impacts { width: 58%; }
    .medium #inspector { width: 100%; }
    .narrow #changes, .narrow #impacts, .narrow #inspector { width: 100%; }
    #tiny { display: none; height: 1fr; padding: 1; }
    Footer { background: #181825; }
    FooterKey { background: #181825; }
    FooterKey > .footer-key--key { background: #313244; color: #b4befe; }
    FooterKey > .footer-key--description { background: #181825; color: #a6adc8; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "pane(1)", "Next pane", priority=True),
        Binding("shift+tab", "pane(-1)", "Previous pane", show=False, priority=True),
        Binding("1", "show_pane(0)", "Changes", show=False),
        Binding("2", "show_pane(1)", "Impacts", show=False),
        Binding("3", "show_pane(2)", "Evidence", show=False),
        Binding("j", "move(1)", "Down", show=False),
        Binding("k", "move(-1)", "Up", show=False),
        Binding("/", "search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("p", "paths", "Path"),
        Binding("s", "side", "Before/after", show=False),
        Binding("t", "tests", "Tests"),
        Binding("d", "diagnostics", "Diagnostics", show=False),
        Binding("f", "filter", "Filter", show=False),
        Binding("c", "location", "Location", show=False),
        Binding("r", "reload", "Refresh", show=False),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self,
        loader: Callable[[bool], ExplorerSession],
        *,
        monochrome: bool = False,
    ):
        super().__init__(ansi_color=monochrome)
        self.console.no_color = monochrome
        self.error_console.no_color = monochrome
        self.loader = loader
        self.session: ExplorerSession | None = None
        self.selected_change: Change | None = None
        self.selected_impact: Impact | None = None
        self.selected_item: Item | None = None
        self.side = "after"
        self.pane_index = 0
        self.layout_mode = "wide"
        self.filter_name = "All"
        self.search_query = ""
        self.error_code = 0
        self.groups: dict[int, list[Item]] = {}
        self.group_offsets: dict[int, int] = {}
        self.source_generation = 0
        self.source_timer: Timer | None = None
        self.search_timer: Timer | None = None
        self.last_excerpt: SourceExcerpt | None = None

    def compose(self) -> ComposeResult:
        yield Static("RIPPLE · immutable change evidence", id="masthead", markup=False)
        yield Static("Loading report…", id="summary", markup=False)
        yield Static(
            "Reading selected Git revisions in a background worker…", id="status", markup=False
        )
        yield Input(placeholder="Search symbol, package or path · Escape clears", id="search")
        yield Static(
            "CHANGES · Tab cycles panes · 1 Changes / 2 Impacts / 3 Evidence",
            id="pane-label",
            markup=False,
        )
        with Horizontal(id="panes"):
            yield DataTable(id="changes", classes="pane", cursor_type="row", zebra_stripes=True)
            yield Tree[Item]("Impact evidence", id="impacts", classes="pane")
            yield DetailLog(
                id="inspector",
                classes="pane",
                wrap=True,
                markup=False,
                highlight=False,
                auto_scroll=False,
                min_width=1,
                max_lines=2000,
            )
        yield Static(
            "Terminal too small. Resize to at least 40 columns × 12 rows. q quits.",
            id="tiny",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#changes", DataTable)
        table.add_column("Change", width=10)
        table.add_column("Symbol", width=40)
        table.border_title = "Changed symbols"
        tree = self.query_one("#impacts", Tree)
        tree.border_title = "Potential effects"
        tree.show_root = False
        self.query_one("#inspector", DetailLog).border_title = "Source & evidence"
        self.apply_layout()
        self.action_show_pane(0)
        self.reload_report(False)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, ModalScreen):
            return False
        if isinstance(self.focused, Input) and action not in {"clear_search", "pane"}:
            return False
        return True

    @work(thread=True, exclusive=True, group="report", exit_on_error=False)
    def reload_report(self, refresh: bool) -> None:
        worker = get_current_worker()
        try:
            session = self.loader(refresh)
            if not worker.is_cancelled:
                self.call_from_thread(self.loaded, session)
        except Exception as exc:
            if not worker.is_cancelled:
                code = exc.exit_code if isinstance(exc, RippleError) else 1
                message = str(exc) if isinstance(exc, RippleError) else type(exc).__name__
                self.call_from_thread(self.failed, message, code)

    def loaded(self, session: ExplorerSession) -> None:
        self.session = session
        self.error_code = 0
        self.selected_change = None
        self.selected_impact = None
        self.last_excerpt = None
        report = session.report
        self.query_one("#masthead", Static).update(
            literal(
                f"RIPPLE {__version__} · {report.comparison.base_ref} → "
                f"{report.comparison.head_ref}\n"
                f"{report.comparison.base_tree[:12]} → {report.comparison.head_tree[:12]}"
            )
        )
        self.query_one("#summary", Static).update(
            literal(
                f"{report.summary['changed_symbol_count']} changed · "
                f"{len(report.impacts)} callers · "
                f"{report.summary.get('related_test_count', 0)} tests · "
                f"{report.completeness.unresolved_references} unresolved"
            )
        )
        self.update_status()
        self.rebuild_changes()
        self.action_show_pane(0)

    def failed(self, message: str, code: int) -> None:
        self.error_code = code
        self.query_one("#status", Static).update(
            literal(
                f"Report load failed: {message}\n"
                "Previous report retained if available. r retries; q exits."
            )
        )

    def update_status(self) -> None:
        if self.session is None:
            return
        report = self.session.report
        notes = ", ".join(report.completeness.reasons) or "within static policy"
        stale = " · STALE: historical evidence" if self.session.stale else ""
        self.query_one("#status", Static).update(
            literal(
                f"{self.session.label}{stale} · {report.completeness.status}\n"
                f"{notes} · filter {self.filter_name} · static evidence is not a safety guarantee"
            )
        )

    def matches(self, name: str, site: Location | None) -> bool:
        return self.search_query in (name + " " + (site.path if site else "")).casefold()

    def rebuild_changes(self) -> None:
        if self.session is None:
            return
        report = self.session.report
        selected_id = self.selected_change.change_id if self.selected_change else None
        table = self.query_one("#changes", DataTable)
        table.clear()
        matching = [
            c
            for c in report.changes
            if self.matches(c.display_name, c.after or c.before)
            and (
                self.filter_name == "All"
                or (self.filter_name == "Modified" and c.kind.endswith("_modified"))
                or (self.filter_name == "Added" and c.kind == "symbol_added")
                or (self.filter_name == "Deleted" and c.kind == "symbol_removed")
                or (self.filter_name == "Relocated" and c.kind == "symbol_relocated")
            )
        ]
        visible = matching[:CHANGE_LIMIT]
        for change in visible:
            table.add_row(
                literal(change.kind.removesuffix("_modified")),
                literal(change.display_name),
                key=change.change_id,
            )
        table.border_subtitle = f"{len(visible)} / {len(matching)} · search to narrow"
        selected = next((c for c in visible if c.change_id == selected_id), None)
        self.selected_change = selected or (visible[0] if visible else None)
        if self.selected_change:
            table.move_cursor(row=visible.index(self.selected_change))
        self.rebuild_impacts()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.session is None:
            return
        change = next(
            (c for c in self.session.report.changes if c.change_id == event.row_key.value), None
        )
        if change is not None and change != self.selected_change:
            self.selected_change = change
            self.rebuild_impacts()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_show_pane(1)

    def rebuild_impacts(self) -> None:
        if self.session is None:
            return
        self.source_generation += 1
        self.selected_impact = None
        self.selected_item = None
        self.groups.clear()
        self.group_offsets.clear()
        tree: Tree[Item] = self.query_one("#impacts", Tree)
        tree.clear()
        report = self.session.report
        origin = self.selected_change.change_id if self.selected_change else None
        direct: list[Item] = []
        indirect: list[Item] = []
        for index, impact in enumerate(report.impacts):
            if (origin is None or origin in impact.origin_change_ids) and self.matches(
                impact.display_name,
                impact.after or impact.before,
            ):
                (direct if impact.depth == 1 else indirect).append(Item("impact", index))
        candidates = [
            Item("candidate", index)
            for index, candidate in enumerate(report.match_candidates)
            if (
                self.selected_change is None
                or self.selected_change.before_symbol_id in candidate.before_symbol_ids
                or self.selected_change.after_symbol_id in candidate.after_symbol_ids
            )
            and self.search_query
            in " ".join((*candidate.before_names, *candidate.after_names)).casefold()
        ]
        unknowns = [
            Item("diagnostic", index)
            for index, diagnostic in enumerate(report.diagnostics)
            if self.search_query in f"{diagnostic['path']} {diagnostic['message']}".casefold()
        ]
        for title, items in (
            ("Direct callers", direct),
            ("Transitive callers", indirect),
            ("Relocation candidates · no call inference", candidates),
            ("Diagnostics · whole comparison", unknowns),
        ):
            key = len(self.groups)
            self.groups[key] = items
            node = tree.root.add(literal(f"{title} ({len(items)})"), Item("group", key))
            if items:
                self.populate(node, key)
                node.expand()
        if not any(self.groups.values()):
            tree.root.add_leaf(literal("No matching effects or diagnostics"))
        tree.root.expand()
        first = next(
            (items[0] for items in (direct, indirect, candidates, unknowns) if items), None
        )
        if first is not None:
            self.inspect_item(first)
            for group in tree.root.children:
                child = next((n for n in group.children if n.data == first), None)
                if child is not None:
                    tree.move_cursor(child)
                    break
        elif self.selected_change is not None:
            self.inspect_change()
        else:
            self.write_inspector("No matching changes or effects. Clear search or change filters.")

    def populate(self, node: TreeNode[Item], key: int) -> None:
        start = self.group_offsets.get(key, 0)
        items = self.groups[key]
        for item in items[start : start + PAGE_SIZE]:
            node.add_leaf(self.item_label(item), item)
        end = min(len(items), start + PAGE_SIZE)
        self.group_offsets[key] = end
        if end < len(items):
            node.add_leaf(
                literal(f"Load next {min(PAGE_SIZE, len(items) - end)} · Enter"), Item("more", key)
            )

    def item_label(self, item: Item) -> Text:
        assert self.session is not None
        report = self.session.report
        if item.kind == "impact":
            impact = report.impacts[item.index]
            return literal(f"{impact.display_name} · {impact.provenance} · depth {impact.depth}")
        if item.kind == "candidate":
            candidate = report.match_candidates[item.index]
            return literal(
                f"CANDIDATE {' / '.join(candidate.before_names)} → "
                f"{' / '.join(candidate.after_names)}"
            )
        diagnostic = report.diagnostics[item.index]
        return literal(f"[{diagnostic['snapshot']}] {diagnostic['code']} · {diagnostic['path']}")

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[Item]) -> None:
        if event.node.data is not None and event.node.data.kind not in {"group", "more"}:
            self.inspect_item(event.node.data)

    def on_tree_node_selected(self, event: Tree.NodeSelected[Item]) -> None:
        item = event.node.data
        if item is None:
            return
        if item.kind == "more" and event.node.parent is not None:
            parent = event.node.parent
            event.node.remove()
            self.populate(parent, item.index)
        elif item.kind != "group":
            self.inspect_item(item)
            self.action_show_pane(2)

    def write_inspector(self, content: str) -> None:
        log = self.query_one("#inspector", DetailLog)
        log.clear()
        log.write(literal(content))

    def inspect_change(self) -> None:
        assert self.session is not None and self.selected_change is not None
        self.last_excerpt = None
        change = self.selected_change
        self.write_inspector(
            f"CHANGED · {change.kind}\n{change.display_name}\n"
            f"Match: {change.match_rule}\n"
            "No retained resolved callers for the selected change.\n"
            "No result does not prove safety."
        )
        site = change.after if self.side == "after" else change.before
        if site is None:
            site = change.before or change.after
            self.side = "before" if change.before is not None else "after"
        if site is not None:
            tree = (
                self.session.report.comparison.base_tree
                if self.side == "before"
                else (self.session.report.comparison.head_tree)
            )
            self.schedule_source(tree, site)

    def inspect_item(self, item: Item) -> None:
        if self.session is None:
            return
        self.selected_item = item
        self.selected_impact = None
        self.source_generation += 1
        self.last_excerpt = None
        report = self.session.report
        if item.kind == "impact":
            impact = report.impacts[item.index]
            self.selected_impact = impact
            paths = [p for p in impact.evidence_paths if p.snapshot == self.side]
            if not paths:
                paths = list(impact.evidence_paths)
                self.side = paths[0].snapshot
            path = paths[0]
            step = path.steps[0]
            self.write_inspector(
                f"RESOLVED STATIC CALL · {impact.provenance}\n"
                f"{impact.display_name}\n{impact.category} · depth {impact.depth}\n"
                f"{step.caller} --CALLS--> {step.callee}\n"
                f"{step.site.path}:{step.site.line}:{step.site.column}\n"
                f"{step.resolver_rule}\n{step.explanation}\n"
                "p full retained paths · s before/after\n"
                "Static evidence is not a runtime guarantee."
            )
            self.schedule_source(path.tree, step.site)
        elif item.kind == "candidate":
            candidate = report.match_candidates[item.index]
            self.write_inspector(
                f"CANDIDATE · relocation only\n"
                f"{' / '.join(candidate.before_names)} → "
                f"{' / '.join(candidate.after_names)}\n"
                f"{candidate.match_rule}\n{candidate.explanation}\n"
                "No identities merged; no call impact inferred."
            )
        elif item.kind == "diagnostic":
            diagnostic = report.diagnostics[item.index]
            self.write_inspector(
                f"DIAGNOSTIC · {diagnostic['snapshot']}\n"
                f"{diagnostic['code']}\n{diagnostic['message']}\n"
                f"{diagnostic['path']}:{diagnostic['line']}\n"
                f"Tree: {diagnostic['tree']}"
            )

    def schedule_source(self, tree: str, site: Location) -> None:
        self.source_generation += 1
        generation = self.source_generation
        if self.source_timer is not None:
            self.source_timer.stop()
        self.source_timer = self.set_timer(0.12, lambda: self.read_source(tree, site, generation))

    @work(thread=True, exclusive=True, group="source", exit_on_error=False)
    def read_source(self, tree: str, site: Location, generation: int) -> None:
        worker = get_current_worker()
        session = self.session
        if worker.is_cancelled or session is None or generation != self.source_generation:
            return
        try:
            excerpt = (
                session.reader.excerpt(tree, site, lambda: worker.is_cancelled)
                if session.reader
                else SourceExcerpt(
                    tree, site, 0, (), False, "No repository attached; source preview unavailable."
                )
            )
        except (RippleError, OSError, ValueError, SyntaxError, UnicodeError) as exc:
            excerpt = SourceExcerpt(
                tree, site, 0, (), False, f"Immutable source unavailable ({type(exc).__name__})."
            )
        if not worker.is_cancelled:
            self.call_from_thread(self.show_excerpt, excerpt, generation)

    def show_excerpt(self, excerpt: SourceExcerpt, generation: int) -> None:
        if generation != self.source_generation:
            return
        self.last_excerpt = excerpt
        log = self.query_one("#inspector", DetailLog)
        log.write(
            literal(
                f"\nSOURCE · {self.side} · {excerpt.tree}\n{excerpt.site.path}:{excerpt.site.line}"
            )
        )
        if excerpt.message:
            log.write(literal(excerpt.message))
        for number, line in enumerate(excerpt.lines, excerpt.first_line):
            value = literal(f"{'>' if number == excerpt.site.line else ' '} {number:4}  {line}")
            if number == excerpt.site.line:
                value.stylize("bold reverse")
            log.write(value)
        if excerpt.truncated:
            log.write(literal("Long source lines clipped; excerpt limited to 8192 bytes."))

    def on_resize(self, event: events.Resize) -> None:
        if self.query("#panes"):
            self.apply_layout((event.size.width, event.size.height))

    def apply_layout(self, size: tuple[int, int] | None = None) -> None:
        width, height = size if size is not None else self.size
        self.layout_mode = "wide" if width >= 120 else ("medium" if width >= 80 else "narrow")
        self.set_class(self.layout_mode == "medium", "medium")
        self.set_class(self.layout_mode == "narrow", "narrow")
        tiny = width < 40 or height < 12
        self.query_one("#tiny").display = tiny
        self.query_one("#panes").display = not tiny
        for index, name in enumerate(("changes", "impacts", "inspector")):
            visible = (
                self.layout_mode == "wide"
                or (self.layout_mode == "medium" and ((self.pane_index == 2) == (index == 2)))
                or (self.layout_mode == "narrow" and index == self.pane_index)
            )
            self.query_one(f"#{name}").display = visible

    def action_show_pane(self, index: int) -> None:
        self.pane_index = index % 3
        self.apply_layout()
        name = ("changes", "impacts", "inspector")[self.pane_index]
        self.query_one(f"#{name}").focus()
        self.query_one("#pane-label", Static).update(
            literal(f"{name.upper()} · Tab cycles panes · 1 Changes / 2 Impacts / 3 Evidence")
        )

    def action_pane(self, direction: int) -> None:
        self.action_show_pane(self.pane_index + direction)

    def action_move(self, direction: int) -> None:
        focused = self.focused
        action = "action_cursor_down" if direction > 0 else "action_cursor_up"
        if isinstance(focused, (DataTable, Tree)):
            getattr(focused, action)()
        elif isinstance(focused, DetailLog):
            (focused.action_scroll_down if direction > 0 else focused.action_scroll_up)()

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.search_query = event.value.casefold().strip()
        if self.search_timer is not None:
            self.search_timer.stop()
        self.search_timer = self.set_timer(0.15, self.rebuild_changes)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_show_pane(1)

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        search.value = ""
        search.display = False
        self.search_query = ""
        self.rebuild_changes()
        self.action_show_pane(self.pane_index)

    def action_paths(self) -> None:
        content = (
            path_text(self.selected_impact.evidence_paths)
            if self.selected_impact
            else ("Select a resolved caller to inspect its retained evidence paths.")
        )
        self.push_screen(DetailScreen("Canonical paths · original revision evidence", content))

    def action_side(self) -> None:
        self.side = "before" if self.side == "after" else "after"
        if self.selected_item is not None:
            self.inspect_item(self.selected_item)
        elif self.selected_change is not None:
            self.inspect_change()

    def action_tests(self) -> None:
        if self.session is None:
            return
        origin = self.selected_change.change_id if self.selected_change else None
        tests = [
            t
            for t in self.session.report.related_tests
            if origin is None or origin in t.origin_change_ids
        ]
        lines = ["Static associations, not sufficient coverage. No tests are run.", ""]
        for test in tests:
            lines.extend(
                (
                    f"{test.display_name} · {test.evidence_class} · {test.provenance}",
                    path_text(test.evidence_paths),
                )
            )
            for imported in test.import_evidence:
                lines.append(
                    f"Import only · {imported.snapshot} · {imported.tree}\n"
                    f"{imported.site.path}:{imported.site.line} · {imported.resolver_rule}"
                )
        if not tests:
            lines.append("No related test associations for this selection.")
        self.push_screen(DetailScreen("Related tests", "\n".join(lines)))

    def action_diagnostics(self) -> None:
        if self.session is None:
            return
        report = self.session.report
        lines = [
            f"Completeness: {report.completeness.status}",
            f"Reasons: {', '.join(report.completeness.reasons) or 'none'}",
            "",
        ]
        for diagnostic in report.diagnostics[:1000]:
            lines.append(
                f"[{diagnostic['snapshot']}] {diagnostic['code']} · "
                f"{diagnostic['path']}:{diagnostic['line']}\n{diagnostic['message']}"
            )
        if len(report.diagnostics) > 1000:
            lines.append(
                "Showing first 1000 diagnostics; search the tree or inspect JSON for more."
            )
        self.push_screen(DetailScreen("Diagnostics · whole comparison", "\n\n".join(lines)))

    def action_filter(self) -> None:
        self.push_screen(FilterScreen(), self.set_filter)

    def set_filter(self, value: str | None) -> None:
        if value:
            self.filter_name = value
            self.update_status()
            self.rebuild_changes()

    def action_location(self) -> None:
        site = self.last_excerpt.site if self.last_excerpt else None
        value = f"{site.path}:{site.line}:{site.column}" if site else "No source location selected."
        self.push_screen(DetailScreen("Location · selectable text", value))

    def action_reload(self) -> None:
        self.query_one("#status", Static).update(
            "Refreshing immutable report in a background worker…"
        )
        self.source_generation += 1
        self.reload_report(True)

    def action_help(self) -> None:
        self.push_screen(
            DetailScreen(
                "Ripple · keyboard reference",
                "Tab / Shift+Tab: switch panes; 1/2/3: choose pane\n"
                "j/k or arrows: navigate; Enter: inspect; Space: expand/collapse tree groups\n"
                "/: global symbol/package/path search; Enter: return to impacts; Escape: clear\n"
                "f: change filter; p: full retained paths; s: before/after source\n"
                "t: related tests; d: diagnostics; c: location text; r: explicit refresh\n"
                "q: quit; Escape or q closes detail screens\n\n"
                "Counts describe the full report; search/filter only affects presentation.\n"
                "Resolved calls are static evidence, not runtime guarantees. Candidates are\n"
                "relocation evidence only. Unknowns do not inflate resolved counts.\n"
                "Before-only paths retain old Git source. No target code runs.\n"
                "Wide: three panes; medium: two panes or evidence; narrow: one selected pane.",
            )
        )

    async def action_quit(self) -> None:
        self.workers.cancel_all()
        self.exit(self.error_code)
