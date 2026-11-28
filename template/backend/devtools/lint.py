import argparse
import os
import subprocess
import sys
import tomllib

from funlog import log_calls
from rich import get_console, print as rprint, reconfigure

PROJECT_ROOT = ".."
DOC_PATHS = ["../README.md", "../docs"]
COMPLEXITY_PATHS = ["app"]
COMPLEXITY_BASELINE = ".complexity-baseline.json"
STARTUP_BASELINE = ".startup-baseline.json"
TYPECHECK_THREADS_MAX = 6


reconfigure(emoji=not get_console().options.legacy_windows)


def read_source_paths() -> list[str]:
    with open("pyproject.toml", "rb") as handle:
        include = tomllib.load(handle)["tool"]["basedpyright"]["include"]
    return [str(path) for path in include]


def typecheck_threads() -> int:
    cpus = os.cpu_count()
    if cpus is None:
        raise RuntimeError(
            "os.cpu_count() returned None. Lint needs the CPU count to set basedpyright threads."
        )
    return min(cpus, TYPECHECK_THREADS_MAX)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run linting and formatting.")
    parser.add_argument("--check", action="store_true", help="Check only, without modifying files.")
    args = parser.parse_args()

    rprint()

    source_paths = read_source_paths()
    errcount = 0
    if args.check:
        errcount += run(["codespell", *source_paths, *DOC_PATHS])
        errcount += run(["ruff", "check", *source_paths])
        errcount += run(["ruff", "format", "--check", *source_paths])
    else:
        errcount += run(["codespell", "--write-changes", *source_paths, *DOC_PATHS])
        errcount += run(["ruff", "check", "--fix", *source_paths])
        errcount += run(["ruff", "format", *source_paths])
    errcount += run(["basedpyright", "--threads", str(typecheck_threads())])
    errcount += run([sys.executable, "devtools/comments.py", "--tree", PROJECT_ROOT])
    errcount += run(
        [
            sys.executable,
            "devtools/complexity.py",
            *COMPLEXITY_PATHS,
            "--baseline",
            COMPLEXITY_BASELINE,
            *([] if args.check else ["--tighten-baseline"]),
        ]
    )
    errcount += run(
        [
            sys.executable,
            "devtools/startup.py",
            "--baseline",
            STARTUP_BASELINE,
            *([] if args.check else ["--tighten-baseline"]),
        ]
    )

    rprint()

    if errcount != 0:
        rprint(f"[bold red]:x: Lint failed with {errcount} errors.[/bold red]")
    else:
        rprint("[bold green]:white_check_mark: Lint passed![/bold green]")
    rprint()

    return errcount


@log_calls(level="warning", show_timing_only=True)
def run(cmd: list[str]) -> int:
    rprint()
    rprint(f"[bold green]>> {' '.join(cmd)}[/bold green]")
    errcount = 0
    try:
        subprocess.run(cmd, text=True, check=True)
    except KeyboardInterrupt:
        rprint("[yellow]Keyboard interrupt - Cancelled[/yellow]")
        errcount = 1
    except subprocess.CalledProcessError as e:
        rprint(f"[bold red]Error: {e}[/bold red]")
        errcount = 1

    return errcount


if __name__ == "__main__":
    sys.exit(main())
