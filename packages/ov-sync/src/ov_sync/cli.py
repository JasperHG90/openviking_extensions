"""The ``ovsync`` command line."""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from types import FrameType
from typing import NoReturn

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .client import OvClient, OvError
from .config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG_TOML,
    OvSyncConfig,
    load_config,
    load_credentials,
)
from .engine import SyncResult, plan_sync, sync
from .state import RootUriMismatch, SyncState

console = Console()
errors = Console(stderr=True)

app = typer.Typer(
    name="ovsync",
    help="Sync a local folder into OpenViking, tracking what was sent locally.",
    no_args_is_help=True,
    add_completion=False,
)

FolderArgument = typer.Argument(..., help="Folder to sync.")
ConfigOption = typer.Option(None, "--config", "-c", help=f"Path to a {CONFIG_FILENAME}.")
RootUriOption = typer.Option(
    None,
    "--root-uri",
    help="Target directory URI. Overrides the config; required the first time.",
)


def _fail(message: str) -> NoReturn:
    """Print an error and exit with a non-zero status."""
    errors.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


def _resolve_folder(folder: Path) -> Path:
    """Return the folder as an absolute path, or exit if it is not a directory."""
    resolved = folder.expanduser().resolve()
    if not resolved.is_dir():
        _fail(f"Not a directory: {resolved}")
    return resolved


def _load(folder: Path, config: Path | None) -> OvSyncConfig:
    """Load the sync configuration, or exit with the reason it could not be read."""
    try:
        return load_config(folder, config)
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))


def _resolve_root_uri(
    root_uri: str | None, config: OvSyncConfig, state: SyncState
) -> str:
    """Work out where this folder syncs to.

    Precedence: the ``--root-uri`` flag, then the config file, then the root
    recorded by the last run. Exits when none of the three supplies one.
    """
    resolved = root_uri or config.sync.root_uri or state.root_uri
    if not resolved:
        _fail(
            "No target URI. Pass --root-uri, or set root_uri under [sync] in "
            f"{CONFIG_FILENAME}."
        )
    return resolved


def _connect() -> OvClient:
    """Build a client from the OpenViking CLI's credentials, or exit."""
    try:
        return OvClient(load_credentials())
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))


def _check_root(client: OvClient, root_uri: str, *, create: bool = False) -> None:
    """Confirm the target exists and is a directory, or exit saying how to make it.

    A missing root is not created unless asked for. A typo in a URI is easy to
    make and hard to spot, and silently creating ``viking://resources/notse``
    would leave a stray tree nobody looks at.

    Parameters
    ----------
    client :
        Connected OpenViking client.
    root_uri :
        The target directory.
    create :
        Create the directory when it is missing, instead of exiting.
    """
    try:
        stat = client.stat(root_uri)
    except OvError as exc:
        _fail(f"Cannot reach OpenViking: {exc}")
    if stat is None and create:
        # Kept out of the block above on purpose: the server refuses some
        # roots outright, and reporting "cannot reach OpenViking" for an
        # answer it gave clearly would send the user looking at the network.
        try:
            client.mkdir(root_uri, description="Synced by ov-sync.")
        except OvError as exc:
            _fail(f"Cannot create {root_uri}: {exc}")
        return
    if stat is None:
        _fail(
            f"{root_uri} does not exist. Create it with --create-root, or:\n"
            f"  ov mkdir {root_uri}"
        )
    elif not stat.is_dir:
        _fail(f"{root_uri} is a file, not a directory. Pick a directory to sync into.")


def _progress() -> Progress:
    """Build the progress display used by run and watch."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TextColumn("{task.fields[detail]}"),
        console=console,
    )


def _report(result: SyncResult) -> None:
    """Print what a run did."""
    if result.created:
        console.print(f"[green]Created:[/green] {result.created}")
    if result.updated:
        console.print(f"[green]Updated:[/green] {result.updated}")
    if result.touched:
        console.print(f"[dim]Touched but unchanged: {result.touched} (not re-sent)[/dim]")
    if result.unchanged:
        console.print(f"[dim]Unchanged: {result.unchanged}[/dim]")
    if result.deferred:
        console.print(f"[dim]Deferred to a later pass: {result.deferred}[/dim]")
    if result.deleted_applied:
        console.print(f"[yellow]Deleted:[/yellow] {result.deleted_applied}")
        if result.snapshot_id:
            console.print(f"  Undo with: ov snapshot restore {result.snapshot_id}")
    elif result.deleted_detected:
        console.print(
            f"[yellow]Gone from the folder:[/yellow] {len(result.deleted_detected)} "
            "still in OpenViking. Re-run with --delete to remove them."
        )
    for skipped in result.skipped:
        console.print(
            f"[yellow]Skipped[/yellow] {skipped.relative_path}: {skipped.reason}"
        )
    for error in result.errors:
        console.print(f"[red]{error}[/red]")
    if is_settled(result):
        console.print("[green]Everything is up to date.[/green]")


def is_settled(result: SyncResult) -> bool:
    """Whether the two sides now match, with nothing left outstanding.

    A pending deletion or a skipped file counts as outstanding: saying
    "everything is up to date" one line after reporting a file that is gone
    locally but still in OpenViking would contradict itself.
    """
    return not (
        result.written
        or result.deleted_applied
        or result.deleted_detected
        or result.skipped
        or result.errors
    )


@app.command()
def init(folder: Path = FolderArgument) -> None:
    """Write a starter ov-sync.toml into a folder."""
    resolved = _resolve_folder(folder)
    path = resolved / CONFIG_FILENAME
    if path.exists():
        _fail(f"{path} already exists.")
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    console.print(f"[green]Created {path}[/green]")
    console.print("Set root_uri under [sync], then run: ovsync run .")


@app.command()
def status(
    folder: Path = FolderArgument,
    config: Path | None = ConfigOption,
    root_uri: str | None = RootUriOption,
) -> None:
    """Show what the next run would do, without contacting the server."""
    resolved = _resolve_folder(folder)
    settings = _load(resolved, config)
    with SyncState(resolved / settings.sync.state_file) as state:
        target = _resolve_root_uri(root_uri, settings, state)
        plan, detail = plan_sync(resolved, settings.sync, state)

        table = Table(title="ov-sync status")
        table.add_column("", style="bold")
        table.add_column("")
        table.add_row("Folder", str(resolved))
        table.add_row("Target", target)
        table.add_row("Last sync", state.last_sync or "never")
        table.add_row("Files found", str(detail.scanned))
        table.add_row("To send", str(len(plan.write)))
        table.add_row("Unchanged", str(plan.unchanged))
        table.add_row("Touched, not changed", str(plan.touched))
        table.add_row("Gone from the folder", str(len(plan.delete)))
        table.add_row("Skipped", str(len(plan.skipped)))
        console.print(table)

        if plan.write:
            console.print("\n[bold]Would send:[/bold]")
            for path in plan.write[:20]:
                console.print(f"  {path}")
            if len(plan.write) > 20:
                console.print(f"  ... and {len(plan.write) - 20} more")
        if plan.delete:
            console.print("\n[bold]Gone from the folder:[/bold]")
            for uri in plan.delete[:20]:
                console.print(f"  {uri}")
            if len(plan.delete) > 20:
                console.print(f"  ... and {len(plan.delete) - 20} more")
        for skipped in plan.skipped:
            console.print(
                f"[yellow]Skipped[/yellow] {skipped.relative_path}: {skipped.reason}"
            )


@app.command()
def run(
    folder: Path = FolderArgument,
    config: Path | None = ConfigOption,
    root_uri: str | None = RootUriOption,
    full: bool = typer.Option(
        False, "--full", help="Re-send every file, ignoring the state."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Say what would happen, contact the server only to check the root.",
    ),
    delete: bool = typer.Option(
        False,
        "--delete",
        help="Remove resources whose local file is gone. Off by default: "
        "a run reports them first.",
    ),
    snapshot: bool = typer.Option(
        True,
        "--snapshot/--no-snapshot",
        help="Commit a snapshot before deleting, so the removal can be undone.",
    ),
    create_root: bool = typer.Option(
        False,
        "--create-root",
        help="Create the target directory if it does not exist yet.",
    ),
) -> None:
    """Send everything that changed since the last run."""
    resolved = _resolve_folder(folder)
    settings = _load(resolved, config)

    with SyncState(resolved / settings.sync.state_file) as state, _connect() as client:
        target = _resolve_root_uri(root_uri, settings, state)
        # A dry run reports; it does not make the directory it reports about.
        _check_root(client, target, create=create_root and not dry_run)

        task_ids: dict[str, TaskID] = {}
        progress = _progress()

        def on_progress(phase: str, done: int, total: int, detail: str) -> None:
            """Mirror the engine's phases into the progress display."""
            if phase not in task_ids:
                labels = {
                    "scanning": "Scanning",
                    "hashing": "Hashing",
                    "writing": "Sending",
                    "deleting": "Deleting",
                    "done": "Done",
                }
                task_ids[phase] = progress.add_task(
                    labels.get(phase, phase), total=total or None, detail=detail
                )
            progress.update(
                task_ids[phase], completed=done, total=total or None, detail=detail
            )

        try:
            with progress:
                result = sync(
                    resolved,
                    target,
                    client,
                    settings.sync,
                    state,
                    full=full,
                    dry_run=dry_run,
                    apply_deletes=delete,
                    snapshot_before_delete=snapshot,
                    on_progress=on_progress,
                )
        except RootUriMismatch as exc:
            _fail(str(exc))

    if dry_run:
        console.print(f"[bold]Would send:[/bold] {len(result.plan.write)} file(s)")
        console.print(f"[bold]Gone from the folder:[/bold] {len(result.plan.delete)}")
        for path in result.plan.write[:20]:
            console.print(f"  {path}")
        if len(result.plan.write) > 20:
            console.print(f"  ... and {len(result.plan.write) - 20} more")
        return

    _report(result)
    if result.errors:
        raise typer.Exit(1)


@app.command()
def watch(
    folder: Path = FolderArgument,
    config: Path | None = ConfigOption,
    root_uri: str | None = RootUriOption,
    mode: str | None = typer.Option(
        None, "--mode", help="Override the watch mode: events or poll."
    ),
    delete: bool = typer.Option(
        False, "--delete", help="Remove resources whose local file is gone."
    ),
) -> None:
    """Keep syncing as the folder changes, until interrupted."""
    from .watcher import (
        FolderWatcher,
    )

    resolved = _resolve_folder(folder)
    settings = _load(resolved, config)
    if mode is not None:
        if mode not in {"events", "poll"}:
            _fail(f"Unknown watch mode: {mode}. Use events or poll.")
        settings.watch.mode = mode

    with SyncState(resolved / settings.sync.state_file) as state:
        target = _resolve_root_uri(root_uri, settings, state)

    with _connect() as client:
        _check_root(client, target)
        watcher = FolderWatcher(
            resolved,
            target,
            client,
            settings,
            apply_deletes=delete,
            on_sync=WatchReporter(),
            on_error=_report_watch_error,
        )

        def handle_signal(signum: int, frame: FrameType | None) -> None:
            """Stop the watcher on Ctrl-C or SIGTERM."""
            watcher.stop()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        console.print(
            f"Watching [cyan]{resolved}[/cyan] -> [cyan]{target}[/cyan] "
            f"({settings.watch.mode} mode). Ctrl-C to stop."
        )
        watcher.run()
        console.print("Stopped.")


class WatchReporter:
    """Prints watch-mode sync results, without repeating itself.

    A watch loop syncs on every save and again on every rescan, so printing
    unconditionally would bury the lines that matter. This prints a run that
    did something, and a standing problem — a pending deletion, a skipped
    file — only when it first appears or changes.

    Attributes
    ----------
    last_outstanding : tuple[str, ...] or None
        Signature of the outstanding items reported last time, so the same
        report is not printed twice.
    """

    def __init__(self) -> None:
        self.last_outstanding: tuple[str, ...] | None = None

    def __call__(self, result: SyncResult) -> None:
        """Report one sync."""
        outstanding = (
            *result.deleted_detected,
            *(skipped.relative_path for skipped in result.skipped),
        )
        acted = bool(result.written or result.deleted_applied or result.errors)
        if not acted and outstanding == self.last_outstanding:
            return
        self.last_outstanding = outstanding
        if acted or outstanding:
            _report(result)


def _report_watch_error(exc: Exception) -> None:
    """Print a sync failure without ending the watch.

    A watched folder on a removable or network volume can vanish for a second.
    The next cycle picks it up again, so the loop reports and carries on.
    """
    errors.print(f"[red]Sync failed, will retry: {exc}[/red]")


def main() -> None:
    """Entry point for the ``ovsync`` console script."""
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover -- interactive only
        errors.print("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
