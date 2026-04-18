#!/usr/bin/env python3
"""Chaos Downloader — CLI for the ProjectDiscovery Chaos API.

Modernized:
  - httpx (async HTTP) instead of urllib
  - asyncio instead of threading/ThreadPoolExecutor
  - rich for progress, tables and colored output
  - questionary for interactive menus
  - type hints and dataclasses
  - cross-platform: Windows, Linux, macOS
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

import httpx
import questionary
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

CHAOS_INDEX_URL = "https://chaos-data.projectdiscovery.io/index.json"
DB_PATH = Path.cwd() / "chaos.db"
USER_AGENT = "Mozilla/5.0 (chaos-downloader)"
MAX_CONCURRENCY = 5
CHUNK_SIZE = 64 * 1024
DOWNLOAD_ATTEMPTS = 3
DB_BATCH = 500

console = Console()


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Program:
    name: str
    url: str
    program_url: str
    platform: str
    bounty: bool
    change: int
    is_new: bool
    count: int
    last_updated: str
    swag: bool

    @classmethod
    def from_api(cls, d: dict) -> "Program":
        return cls(
            name=d["name"],
            url=d["URL"],
            program_url=d.get("program_url", "") or "",
            platform=d.get("platform", "") or "",
            bounty=bool(d.get("bounty", False)),
            change=int(d.get("change", 0) or 0),
            is_new=bool(d.get("is_new", False)),
            count=int(d.get("count", 0) or 0),
            last_updated=d.get("last_updated", "") or "",
            swag="swag" in d,
        )


_FORBIDDEN_FS_CHARS = '<>:"/\\|?*'


def _safe_name(name: str) -> str:
    """Sanitize a program name for use as a filesystem path component."""
    cleaned = "".join("_" if c in _FORBIDDEN_FS_CHARS or ord(c) < 32 else c for c in name)
    cleaned = cleaned.strip(" .")
    return cleaned or "_"


# --------------------------------------------------------------------------- #
# Database                                                                    #
# --------------------------------------------------------------------------- #
class Database:
    def __init__(self, path: Path) -> None:
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._setup()

    def _setup(self) -> None:
        c = self._conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS names ("
            "ID INTEGER PRIMARY KEY, name TEXT UNIQUE, platform TEXT, "
            "offer_bounty BOOLEAN, late_update DATE);"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS subdomains ("
            "ID INTEGER PRIMARY KEY, subdomain TEXT UNIQUE, program_ID INTEGER, "
            "FOREIGN KEY(program_ID) REFERENCES names(ID));"
        )
        self._conn.commit()

    async def insert_program(self, name: str, platform: str, bounty: bool) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO names(name, platform, offer_bounty, late_update) "
                "VALUES(?, ?, ?, DATE('NOW')) "
                "ON CONFLICT(name) DO UPDATE SET "
                "platform=excluded.platform, "
                "offer_bounty=excluded.offer_bounty, "
                "late_update=excluded.late_update;",
                (name, platform, bounty),
            )
            self._conn.commit()

    async def insert_subdomains(
        self, program_name: str, subs: Iterable[str]
    ) -> list[str]:
        """Insert subdomains and return the ones that did NOT exist before."""
        unique = list(dict.fromkeys(s for s in subs if s))
        if not unique:
            return []
        async with self._lock:
            row = self._conn.execute(
                "SELECT ID FROM names WHERE name=?", (program_name,)
            ).fetchone()
            if not row:
                return []
            pid = row[0]

            existing: set[str] = set()
            for i in range(0, len(unique), DB_BATCH):
                chunk = unique[i : i + DB_BATCH]
                ph = ",".join("?" * len(chunk))
                existing.update(
                    r[0]
                    for r in self._conn.execute(
                        f"SELECT subdomain FROM subdomains "
                        f"WHERE program_ID=? AND subdomain IN ({ph})",
                        (pid, *chunk),
                    )
                )

            new_subs = [s for s in unique if s not in existing]
            if new_subs:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO subdomains(subdomain, program_ID) VALUES(?, ?)",
                    [(s, pid) for s in new_subs],
                )
                self._conn.commit()
            return new_subs

    async def get_subdomains(self, program_name: str) -> list[str]:
        async with self._lock:
            row = self._conn.execute(
                "SELECT ID FROM names WHERE name=?", (program_name,)
            ).fetchone()
            if not row:
                return []
            cur = self._conn.execute(
                "SELECT subdomain FROM subdomains WHERE program_ID=?", (row[0],)
            )
            return [r[0] for r in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------- #
# Download pipeline                                                           #
# --------------------------------------------------------------------------- #
def _encode_url(url: str) -> str:
    parts = urlsplit(url)
    # safe="/%" preserves already-encoded sequences so we don't double-escape.
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%"),
            parts.query,
            parts.fragment,
        )
    )


def _extract_zip(zip_path: Path, out_dir: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out_dir)
    finally:
        try:
            zip_path.unlink()
        except FileNotFoundError:
            pass


def _clear_stale_txt(program_dir: Path) -> None:
    """Remove leftover extracted *.txt files so a re-download doesn't re-aggregate."""
    for stale in program_dir.glob("*.txt"):
        try:
            stale.unlink()
        except OSError:
            pass


async def _download_with_retry(
    client: httpx.AsyncClient, url: str, dest: Path
) -> None:
    last_err: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        f.write(chunk)
            return
        except (httpx.HTTPError, OSError) as e:
            last_err = e
            if attempt < DOWNLOAD_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s...
    assert last_err is not None
    raise last_err


async def _merge_and_store(
    program_dir: Path,
    save_dir: Path,
    program_name: str,
    db: Database,
    file_lock: asyncio.Lock,
) -> tuple[int, int, int]:
    """Aggregate extracted subdomains, persist, return (total, new_count, txt_count)."""
    all_subs: list[str] = []
    txt_count = 0
    for txt in program_dir.rglob("*.txt"):
        txt_count += 1
        with txt.open("r", encoding="utf-8", errors="replace") as fh:
            all_subs.extend(line.strip() for line in fh if line.strip())

    if not all_subs:
        return 0, 0, txt_count

    # Dedupe within this run (preserve first-seen order).
    all_subs = list(dict.fromkeys(all_subs))
    new_subs = await db.insert_subdomains(program_name, all_subs)

    master = Path(f"{save_dir.name}.txt")
    new_file = Path(f"new_{save_dir.name}.txt")
    async with file_lock:
        with master.open("a", encoding="utf-8") as m:
            m.write("\n".join(all_subs) + "\n")
        if new_subs:
            with new_file.open("a", encoding="utf-8") as n:
                n.write("\n".join(new_subs) + "\n")

    return len(all_subs), len(new_subs), txt_count


async def _download_one(
    client: httpx.AsyncClient,
    program: Program,
    save_dir: Path,
    db: Database,
    file_lock: asyncio.Lock,
) -> None:
    safe = _safe_name(program.name)
    program_dir = save_dir / safe
    program_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_txt(program_dir)
    await db.insert_program(program.name, program.platform, program.bounty)

    file_name = Path(urlparse(program.url).path).name or f"{safe}.zip"
    zip_path = program_dir / file_name

    try:
        await _download_with_retry(client, _encode_url(program.url), zip_path)
        zip_bytes = zip_path.stat().st_size if zip_path.exists() else 0
        await asyncio.to_thread(_extract_zip, zip_path, program_dir)
        total, new_count, txt_count = await _merge_and_store(
            program_dir, save_dir, program.name, db, file_lock
        )
        if zip_bytes >= 1024 * 1024:
            zip_str = f"{zip_bytes / (1024 * 1024):.1f} MB"
        elif zip_bytes >= 1024:
            zip_str = f"{zip_bytes / 1024:.1f} KB"
        else:
            zip_str = f"{zip_bytes} B"
        console.print(
            f"[red][+][/red] [white]{program.name}[/white] done "
            f"[green]\u2713[/green] "
            f"[dim](zip={zip_str}, files={txt_count}, "
            f"subs={total}, new={new_count})[/dim]"
        )
    except Exception as e:
        console.print(f"[red]Error processing {program.name}: {e}[/red]")


async def download_programs(
    programs: list[Program],
    save_dir: Path,
    db: Database,
) -> None:
    if not programs:
        console.print("[yellow]No programs match that filter.[/yellow]")
        return

    save_dir.mkdir(parents=True, exist_ok=True)
    # Fresh outputs per batch — prevents unbounded growth across re-runs.
    Path(f"{save_dir.name}.txt").write_text("", encoding="utf-8")
    Path(f"new_{save_dir.name}.txt").write_text("", encoding="utf-8")

    file_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
        http2=False,
    ) as client:
        with progress:
            task_id = progress.add_task("Downloading", total=len(programs))

            async def worker(p: Program) -> None:
                async with semaphore:
                    try:
                        await _download_one(client, p, save_dir, db, file_lock)
                    finally:
                        progress.advance(task_id)

            await asyncio.gather(*(worker(p) for p in programs))


# --------------------------------------------------------------------------- #
# Application / Menu                                                          #
# --------------------------------------------------------------------------- #
Predicate = Callable[[Program], bool]


class ChaosDownloader:
    STYLE = questionary.Style(
        [
            ("qmark", "fg:red bold"),
            ("question", "fg:red bold"),
            ("answer", "fg:yellow"),
            ("pointer", "fg:red bold"),
            ("highlighted", "bg:red fg:yellow"),
            ("selected", "fg:yellow"),
            ("separator", "fg:#cc5454"),
            ("instruction", ""),
            ("text", ""),
            ("disabled", "fg:#858585 italic"),
        ]
    )

    TITLE = r"""
  _____ _                       _____                      _                 _
 / ____| |                     |  __ \                    | |               | |
| |    | |__   __ _  ___  ___  | |  | | _____      ___ __ | | ___   __ _  __| | ___ _ __
| |    | '_ \ / _` |/ _ \/ __| | |  | |/ _ \ \ /\ / / '_ \| |/ _ \ / _` |/ _` |/ _ \ '__|
| |____| | | | (_| | (_) \__ \ | |__| | (_) \ V  V /| | | | | (_) | (_| | (_| |  __/ |
 \_____|_| |_|\__,_|\___/|___/ |_____/ \___/ \_/\_/ |_| |_|_|\___/ \__,_|\__,_|\___|_|

Chaos API client — https://chaos.projectdiscovery.io/
"""

    def __init__(self) -> None:
        self.db = Database(DB_PATH)
        self.programs: list[Program] = []

    # -- lifecycle ---------------------------------------------------------- #
    async def load(self) -> None:
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT}, timeout=30.0
            ) as client:
                resp = await client.get(CHAOS_INDEX_URL)
                resp.raise_for_status()
                data = resp.json()
            self.programs = [Program.from_api(d) for d in data]
        except Exception as e:
            console.print(f"[red]Error loading data from API: {e}[/red]")
            self.programs = []

    def _banner(self) -> None:
        console.print(f"[red]{self.TITLE}[/red]")

    def _clear(self) -> None:
        console.clear()

    # -- helpers ------------------------------------------------------------ #
    async def _run(self, predicate: Predicate, save_dir: str) -> None:
        filtered = [p for p in self.programs if predicate(p)]
        console.print(
            f"Starting download of [yellow]{len(filtered)}[/yellow] program(s)..."
        )
        await download_programs(filtered, Path(save_dir), self.db)

    async def _select_platform(self) -> str | None:
        choices = ["Hackerone", "Bugcrowd", "Yeswehack", "Self hosted", "Back to Main Menu"]
        ans = await questionary.select(
            "Select Platform:", choices=choices, style=self.STYLE
        ).ask_async()
        if ans is None or ans == "Back to Main Menu":
            return None
        return "" if ans == "Self hosted" else ans

    @staticmethod
    def _by_platform(p: Program, plat: str) -> bool:
        return p.platform.lower() == plat.lower()

    @staticmethod
    def _plat_dir(plat: str) -> str:
        return plat if plat else "self_hosted"

    # -- actions ------------------------------------------------------------ #
    async def act_all(self) -> None:
        await self._run(lambda _p: True, "all_programmes")

    async def act_bounty(self) -> None:
        await self._run(lambda p: p.bounty, "offer_bounty")

    async def act_no_bounty(self) -> None:
        await self._run(lambda p: not p.bounty, "not_offer_bounty")

    async def act_platform(self) -> None:
        plat = await self._select_platform()
        if plat is None:
            return
        await self._run(lambda p: self._by_platform(p, plat), self._plat_dir(plat))

    async def act_new(self) -> None:
        await self._run(lambda p: p.change != 0, "new_subdomains")

    async def act_new_bounty(self) -> None:
        await self._run(
            lambda p: p.change != 0 and p.bounty, "new_subdomains_and_offer_bounty"
        )

    async def act_new_bounty_platform(self) -> None:
        plat = await self._select_platform()
        if plat is None:
            return
        await self._run(
            lambda p: p.change != 0 and p.bounty and self._by_platform(p, plat),
            f"new_subdomain_and_offer_bounty_and_{self._plat_dir(plat)}",
        )

    async def act_new_platform(self) -> None:
        plat = await self._select_platform()
        if plat is None:
            return
        await self._run(
            lambda p: p.change != 0 and self._by_platform(p, plat),
            f"new_subdomain_and_platform_{self._plat_dir(plat)}",
        )

    async def act_new_no_bounty(self) -> None:
        await self._run(
            lambda p: p.change != 0 and not p.bounty,
            "new_subdomain_and_not_offer_bounty",
        )

    async def act_new_no_bounty_platform(self) -> None:
        plat = await self._select_platform()
        if plat is None:
            return
        await self._run(
            lambda p: p.change != 0 and not p.bounty and self._by_platform(p, plat),
            f"new_subdomain_and_not_offer_bounty_and_{self._plat_dir(plat)}",
        )

    async def act_bounty_platform(self) -> None:
        plat = await self._select_platform()
        if plat is None:
            return
        await self._run(
            lambda p: p.bounty and self._by_platform(p, plat),
            f"offer_bounty_and_{self._plat_dir(plat)}",
        )

    async def act_no_bounty_platform(self) -> None:
        plat = await self._select_platform()
        if plat is None:
            return
        await self._run(
            lambda p: not p.bounty and self._by_platform(p, plat),
            f"not_offer_bounty_and_{self._plat_dir(plat)}",
        )

    async def act_specific(self) -> None:
        options = sorted(p.name for p in self.programs)
        selected = await questionary.checkbox(
            "Select Programs (Space to select, Enter to confirm):",
            choices=options,
            style=self.STYLE,
        ).ask_async()
        if not selected:
            return

        for name in selected:
            match = next((p for p in self.programs if p.name == name), None)
            if not match:
                console.print(f"[red]Program not found: {name}[/red]")
                continue
            self._print_program_info(match)
            await self._run(lambda p, n=name: p.name == n, _safe_name(name))

    def _print_program_info(self, p: Program) -> None:
        table = Table(title=p.name, show_header=True, header_style="bold red")
        table.add_column("Info", style="white")
        table.add_column("Value", style="yellow")
        table.add_row("program url", p.program_url or "-")
        table.add_row("download url", p.url)
        table.add_row("total subdomains", str(p.count))
        table.add_row("new subdomains", str(p.change))
        table.add_row("is new", str(p.is_new))
        table.add_row("platform", p.platform or "self hosted")
        table.add_row("offer reward", str(p.bounty))
        table.add_row("last_updated", p.last_updated[:10])
        console.print(table)

    def act_info(self) -> None:
        if not self.programs:
            console.print("[red]No data available.[/red]")
            return

        metrics: list[tuple[str, str]] = [
            ("Programs last updated", self.programs[0].last_updated[:10]),
            ("Subdomains", str(sum(p.count for p in self.programs))),
            ("Programs", str(len(self.programs))),
            ("Programs changed", str(sum(1 for p in self.programs if p.change != 0))),
            ("New programs", str(sum(1 for p in self.programs if p.is_new))),
            (
                "Hackerone programs",
                str(sum(1 for p in self.programs if p.platform.lower() == "hackerone")),
            ),
            (
                "Bugcrowd programs",
                str(sum(1 for p in self.programs if p.platform.lower() == "bugcrowd")),
            ),
            (
                "Yeswehack programs",
                str(sum(1 for p in self.programs if p.platform.lower() == "yeswehack")),
            ),
            (
                "Self-hosted programs",
                str(sum(1 for p in self.programs if p.platform == "")),
            ),
            ("Programs with rewards", str(sum(1 for p in self.programs if p.bounty))),
            ("Programs with swag", str(sum(1 for p in self.programs if p.swag))),
            (
                "Programs without rewards",
                str(sum(1 for p in self.programs if not p.bounty)),
            ),
        ]

        table = Table(title="Info", show_header=True, header_style="bold red")
        table.add_column("Metric", style="white")
        table.add_column("Value", style="yellow")
        for k, v in metrics:
            table.add_row(k, v)
        console.print(table)

    async def act_export(self) -> None:
        names = sorted(p.name for p in self.programs)
        selected = await questionary.checkbox(
            "Select programs to export (Space to select, Enter to confirm):",
            choices=names,
            style=self.STYLE,
        ).ask_async()
        if not selected:
            return

        for prog in selected:
            subs = await self.db.get_subdomains(prog)
            if not subs:
                console.print(f"[yellow]No subdomains found for {prog} in DB.[/yellow]")
                continue
            out = Path(f"{_safe_name(prog)}_exported.txt")
            with out.open("w", encoding="utf-8") as f:
                f.write("\n".join(subs) + "\n")
            console.print(f"[green]Exported {prog} -> {out}[/green]")

    # -- main loop ---------------------------------------------------------- #
    async def run(self) -> None:
        self._clear()
        self._banner()
        await self.load()

        menu: list[tuple[str, Callable[[], Awaitable[None] | None]]] = [
            ("All Programmes", self.act_all),
            ("Offer Bounty", self.act_bounty),
            ("Not Offer Bounty", self.act_no_bounty),
            ("Platform", self.act_platform),
            ("New Subdomain", self.act_new),
            ("New Subdomain and Offer Bounty", self.act_new_bounty),
            ("New Subdomain and Offer Bounty and Platform", self.act_new_bounty_platform),
            ("New Subdomain and Platform", self.act_new_platform),
            ("New Subdomain and Not Offer Bounty", self.act_new_no_bounty),
            ("New Subdomain and Not Offer Bounty and Platform", self.act_new_no_bounty_platform),
            ("Offer Bounty and Platform", self.act_bounty_platform),
            ("Not Offer Bounty and Platform", self.act_no_bounty_platform),
            ("Specific Programs", self.act_specific),
            ("Info about Programs", self.act_info),
            ("Export Programme from Database", self.act_export),
            ("Quit", None),
        ]
        label_to_action = {label: action for label, action in menu}

        try:
            while True:
                choice = await questionary.select(
                    "Main Menu",
                    choices=[label for label, _ in menu],
                    style=self.STYLE,
                ).ask_async()
                if choice is None or choice == "Quit":
                    break

                action = label_to_action.get(choice)
                if action is None:
                    continue
                try:
                    result = action()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")

                try:
                    input("\nPress Enter to continue...")
                except EOFError:
                    break
                self._clear()
                self._banner()
        finally:
            self.db.close()


# --------------------------------------------------------------------------- #
# Entrypoint                                                                  #
# --------------------------------------------------------------------------- #
def main() -> None:
    # Windows: Proactor loop is fine for httpx; keep default to avoid surprises.
    try:
        asyncio.run(ChaosDownloader().run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
