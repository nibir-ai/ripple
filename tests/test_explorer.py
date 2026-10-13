from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from ripple.analysis import analyze
from ripple.cache import Cache
from ripple.errors import RippleError
from ripple.explorer import DetailScreen, ExplorerApp, FilterScreen, Item, literal, path_text
from ripple.explorer_data import ExplorerSession, SnapshotReader, SourceExcerpt, load_session
from ripple.git_source import GitSource
from ripple.models import Location, Policy


def build_report(repo, *, unknown=False):
    source = "def f(): return 1\ndef g(): return f()\ndef h(): return g()\n"
    if unknown:
        source += "def dynamic(obj): return obj.method()\n"
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    return analyze(repo.root, "HEAD~1", "HEAD", Policy())


async def ready(app, pilot):
    for _ in range(100):
        if app.session is not None or app.error_code:
            break
        await pilot.pause(0.05)
    assert app.session is not None, app.error_code
    await pilot.pause(0.3)
    await app.workers.wait_for_complete()


def test_keyboard_selection_source_paths_modals_and_search(repo):
    report = build_report(repo, unknown=True)
    session = ExplorerSession(report, SnapshotReader(GitSource(repo.root)), "TEST SESSION")

    async def scenario():
        app = ExplorerApp(lambda _: session)
        async with app.run_test(size=(140, 40)) as pilot:
            await ready(app, pilot)
            assert app.selected_change == report.changes[0]
            assert app.selected_impact == report.impacts[0]
            assert (
                app.last_excerpt.site.line == report.impacts[0].evidence_paths[0].steps[0].site.line
            )
            await pilot.press("enter")
            assert app.pane_index == 1
            await pilot.press("down", "enter")
            assert app.pane_index == 2
            await pilot.press("p")
            assert isinstance(app.screen, DetailScreen)
            assert "--CALLS-->" in app.screen.content
            pane = app.pane_index
            await pilot.press("tab")
            assert app.pane_index == pane
            await pilot.press("escape", "d")
            assert "unresolved" in app.screen.content
            await pilot.press("escape", "question_mark")
            assert "Tab" in app.screen.content
            await pilot.press("escape", "/", "q", "j", "t")
            assert app.query_one("#search").value == "qjt"
            assert app.is_running
            await pilot.press("escape")
            assert app.search_query == ""
            await pilot.press("q")
        assert app.return_value == 0

    asyncio.run(scenario())


def test_empty_comparison_and_related_test_evidence(repo):
    source = "def f(): return 1\n"
    repo.commit(
        {
            "m.py": source,
            "tests/test_m.py": "from m import f\ndef test_f(): assert f() == 1\n",
            "tests/test_import.py": "import m\n",
        }
    )
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    populated = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    empty = analyze(repo.root, "HEAD", "HEAD", Policy())

    async def scenario():
        app = ExplorerApp(lambda _: ExplorerSession(populated, None, "REPORT"))
        async with app.run_test(size=(130, 35)) as pilot:
            await ready(app, pilot)
            await pilot.press("t")
            assert "tests.test_m.test_f" in app.screen.content
            assert "--CALLS-->" in app.screen.content
            assert "Import only" in app.screen.content
            await pilot.press("escape")
            app.loaded(ExplorerSession(empty, None, "EMPTY REPORT"))
            await pilot.pause()
            assert app.selected_change is None
            assert app.selected_impact is None
            assert app.query_one("#changes").row_count == 0
            await pilot.press("p")
            assert "Select a resolved caller" in app.screen.content
            await pilot.press("escape", "d")
            assert "Completeness: complete" in app.screen.content
            await pilot.press("escape", "q")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "size,mode",
    [((140, 35), "wide"), ((100, 30), "medium"), ((60, 24), "narrow"), ((30, 8), "narrow")],
)
def test_responsive_pane_switches_and_selection_retention(repo, size, mode):
    report = build_report(repo)
    session = ExplorerSession(report, None, "EXPORTED REPORT")

    async def scenario():
        app = ExplorerApp(lambda _: session, monochrome=True)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            assert app.layout_mode == mode
            original = app.selected_change
            if size[0] < 40:
                assert app.query_one("#tiny").display
                assert not app.query_one("#panes").display
            else:
                await pilot.press("tab", "tab")
                assert app.pane_index == 2
                if mode != "wide":
                    assert not app.query_one("#changes").display
                    assert app.query_one("#inspector").display
                    assert app.query_one("#inspector").region.width == size[0]
                await pilot.resize_terminal(130, 36)
                await pilot.pause()
                assert app.layout_mode == "wide"
                assert all(
                    app.query_one("#" + name).display
                    for name in ("changes", "impacts", "inspector")
                )
                assert app.selected_change == original
            assert app.console.no_color
            await pilot.press("q")

    asyncio.run(scenario())


def test_snapshot_source_is_not_dirty_working_tree_or_symlink(repo):
    report = build_report(repo)
    reader = SnapshotReader(GitSource(repo.root))
    (repo.root / "m.py").write_text("DIRTY SECRET\n")
    site = report.impacts[0].evidence_paths[0].steps[0].site
    excerpt = reader.excerpt(report.comparison.head_tree, site)
    assert "DIRTY SECRET" not in "\n".join(excerpt.lines)
    assert "return f()" in "\n".join(excerpt.lines)
    assert reader.excerpt(report.comparison.head_tree, replace(site, path="../secret.py")).message
    with pytest.raises(ValueError):
        reader.excerpt("--bad-option", site)
    assert reader.excerpt(report.comparison.head_tree, replace(site, line=999)).message
    assert (
        reader.excerpt(report.comparison.head_tree, site, lambda: True).message
        == "Preview cancelled."
    )


def test_source_encoding_long_lines_and_cache_bound(repo):
    sources = {
        f"m{i}.py": "# coding: latin-1\n# café\nvalue=1\n".encode("latin-1") for i in range(10)
    }
    sources["long.py"] = ("#" + "x" * 10000 + "\n") * 6
    repo.commit(sources)
    reader = SnapshotReader(GitSource(repo.root))
    tree = reader.source.resolve("HEAD")
    for index in range(10):
        excerpt = reader.excerpt(tree, Location(f"m{index}.py", 3, 0, 3, 7))
        assert "café" in "\n".join(excerpt.lines)
    assert len(reader.blobs) == 8
    excerpt = reader.excerpt(tree, Location("long.py", 4, 9000, 4, 9050))
    assert excerpt.truncated
    assert len("\n".join(excerpt.lines).encode()) <= 8192
    assert len(excerpt.lines) == 6


def test_saved_report_stale_selection_and_explicit_refresh(repo, tmp_path):
    report = build_report(repo)
    session = load_session(repo.root)
    assert session.report == report
    assert session.label == "CURRENT SAVED ANALYSIS"
    with pytest.raises(RippleError, match="unavailable") as missing:
        load_session(repo.root, analysis_id="missing")
    assert missing.value.exit_code == 2
    analysis_id = Cache(repo.root).status()["last_analysis"]["analysis_id"]
    repo.commit({"m.py": "def f(): return 3\ndef g(): return f()\ndef h(): return g()\n"})
    with pytest.raises(RippleError, match="stale"):
        load_session(repo.root, analysis_id=analysis_id)
    stale = load_session(repo.root, analysis_id=analysis_id, allow_stale=True)
    assert stale.stale
    assert stale.report.comparison.head_tree == report.comparison.head_tree
    refreshed = load_session(repo.root, refresh=True)
    assert refreshed.report.comparison.head_tree != report.comparison.head_tree
    exported = tmp_path / "report.json"
    exported.write_text(report.to_json())
    empty = tmp_path.parent / (tmp_path.name + "-outside")
    empty.mkdir()
    portable = load_session(empty, report_path=exported)
    assert portable.reader is None
    assert portable.report == report


def test_cli_noninteractive_has_no_escape_output_or_cache_work(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "ripple", "explore", "--repo", str(tmp_path)],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"analyze --format json" in result.stderr
    assert b"\x1b" not in result.stderr


def test_literal_and_path_detail_escape_controls_and_markup(repo):
    value = literal("[bold]not markup[/bold]\x1b[31m\u202e")
    assert value.plain.startswith("[bold]not markup[/bold]")
    assert not value.spans
    assert "\x1b" not in value.plain and "\u202e" not in value.plain
    assert literal("first\nsecond\x1b").plain == "first\nsecond\\x1b"
    report = build_report(repo)
    paths = path_text(report.impacts[-1].evidence_paths)
    assert report.comparison.head_tree in paths
    assert "--CALLS-->" in paths


def test_filter_search_empty_and_related_test_views(repo):
    report = build_report(repo)
    session = ExplorerSession(report, None, "REPORT", stale=True)

    async def scenario():
        app = ExplorerApp(lambda _: session)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            assert "STALE" in str(app.query_one("#status").render())
            await pilot.press("f")
            assert isinstance(app.screen, FilterScreen)
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert app.filter_name == "Added"
            assert app.query_one("#changes").row_count == 0
            await pilot.press("t")
            assert "No related test" in app.screen.content
            await pilot.press("escape")
            app.set_filter("All")
            await pilot.press("/", "m", ".", "h")
            await pilot.pause(0.2)
            assert app.query_one("#changes").row_count == 0
            assert app.selected_impact.display_name == "m.h"
            await pilot.press("enter", "p")
            assert "m.h --CALLS--> m.g" in app.screen.content
            await pilot.press("escape", "escape", "q")

    asyncio.run(scenario())


def test_change_filters_match_real_added_deleted_and_modified_symbols(repo):
    repo.commit({"m.py": "def removed(): return 1\ndef modified(): return 1\n"})
    repo.commit({"m.py": "def added(): return 1\ndef modified(): return 2\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())

    async def scenario():
        app = ExplorerApp(lambda _: ExplorerSession(report, None, "REPORT"))
        async with app.run_test(size=(130, 35)) as pilot:
            await ready(app, pilot)
            for kind, names in (
                ("Added", ["m.added"]),
                ("Deleted", ["m.removed"]),
                ("Modified", ["m", "m.modified"]),
            ):
                app.set_filter(kind)
                await pilot.pause()
                table = app.query_one("#changes")
                assert [table.get_row_at(i)[1].plain for i in range(table.row_count)] == names
                assert app.selected_change.display_name == names[0]
            app.set_filter("Relocated")
            assert app.query_one("#changes").row_count == 0
            await pilot.press("q")

    asyncio.run(scenario())


def test_before_only_source_is_old_git_source_after_deletion(repo):
    repo.commit({"m.py": "def f(): return 1\ndef g(): return f()\n"})
    repo.commit({"m.py": "def g(): return 99\n"})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())
    session = ExplorerSession(report, SnapshotReader(GitSource(repo.root)), "REPORT")

    async def scenario():
        app = ExplorerApp(lambda _: session)
        async with app.run_test(size=(130, 35)) as pilot:
            await ready(app, pilot)
            row = next(i for i, change in enumerate(report.changes) if change.display_name == "m.f")
            app.query_one("#changes").move_cursor(row=row)
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()
            assert app.selected_impact.provenance == "before_only"
            assert app.side == "before"
            assert app.last_excerpt.tree == report.comparison.base_tree
            assert "return f()" in "\n".join(app.last_excerpt.lines)
            assert "return 99" not in "\n".join(app.last_excerpt.lines)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert app.side == "before"
            await pilot.press("q")

    asyncio.run(scenario())


def test_pagination_retains_real_report_results(repo):
    source = "def f(): return 1\n" + "".join(f"def c{i}(): return f()\n" for i in range(230))
    repo.commit({"m.py": source})
    repo.commit({"m.py": source.replace("return 1", "return 2")})
    report = analyze(repo.root, "HEAD~1", "HEAD", Policy())

    async def scenario():
        app = ExplorerApp(lambda _: ExplorerSession(report, None, "REPORT"))
        async with app.run_test(size=(130, 35)) as pilot:
            await ready(app, pilot)
            group = app.query_one("#impacts").root.children[0]
            assert len(group.children) == 201
            more = group.children[-1]
            assert more.data.kind == "more"
            app.query_one("#impacts").move_cursor(more)
            await pilot.press("2", "enter")
            assert len(group.children) == 230
            assert len(report.impacts) == 230
            assert all(node.data.kind == "impact" for node in group.children)
            await pilot.press("q")

    asyncio.run(scenario())


def test_worker_error_keeps_terminal_quittable_and_returns_error():
    def failed(_):
        raise RippleError("RPL-GIT-002", "fixture Git failure", 3)

    async def scenario():
        app = ExplorerApp(failed)
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete()
            assert app.error_code == 3
            assert app.session is None
            await pilot.press("question_mark")
            assert isinstance(app.screen, DetailScreen)
            await pilot.press("escape", "q")
        assert app.return_value == 3

    asyncio.run(scenario())


def test_cancelled_source_cannot_overwrite_a_new_selection(repo):
    report = build_report(repo)
    started = threading.Event()
    release = threading.Event()

    class SlowReader(SnapshotReader):
        def excerpt(self, tree, site, cancelled=None):
            if site.line == 2:
                started.set()
                release.wait(3)
            return SourceExcerpt(tree, site, site.line, (f"location {site.line}",), False)

    async def scenario():
        app = ExplorerApp(
            lambda _: ExplorerSession(report, SlowReader(GitSource(repo.root)), "REPORT")
        )
        try:
            async with app.run_test(size=(130, 35)) as pilot:
                for _ in range(100):
                    if started.is_set():
                        break
                    await pilot.pause(0.03)
                assert started.is_set()
                app.inspect_item(Item("impact", 1))
                await pilot.pause(0.3)
                assert app.last_excerpt.site.line == 3
                release.set()
                await pilot.pause(0.2)
                assert app.last_excerpt.site.line == 3
                await pilot.press("q")
        finally:
            release.set()

    asyncio.run(scenario())


def test_cancelled_load_does_not_replace_new_report_and_search_is_responsive(repo):
    original = build_report(repo)
    fresh = replace(original, tool={**original.tool, "fixture": "fresh"})
    started = threading.Event()
    release = threading.Event()

    def loader(refresh):
        if not refresh:
            started.set()
            release.wait(3)
        return ExplorerSession(fresh if refresh else original, None, "REPORT")

    async def scenario():
        app = ExplorerApp(loader)
        try:
            async with app.run_test(size=(130, 35)) as pilot:
                await pilot.pause(0.1)
                assert started.is_set()
                await pilot.press("/", "x", "escape", "r")
                await ready(app, pilot)
                assert app.session.report == fresh
                release.set()
                await pilot.pause(0.2)
                assert app.session.report == fresh
                await pilot.press("q")
        finally:
            release.set()

    asyncio.run(scenario())
