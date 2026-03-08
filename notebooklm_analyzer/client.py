"""Subprocess wrapper to call notebooklm-py."""

import subprocess
import sys
import textwrap

from utils.config_loader import load_config


def run_notebooklm_command(script: str) -> str:
    """Execute a self-contained async Python script and return stdout.

    Uses the python_cmd from config (defaults to current interpreter).

    Args:
        script: A complete Python script string. It must be self-contained
                (imports, async def main(), asyncio.run(main())).

    Returns:
        The stdout output of the script as a string.

    Raises:
        RuntimeError: If the subprocess exits with a non-zero return code.
    """
    # Dedent in case the caller passes indented heredoc-style strings
    script = textwrap.dedent(script)

    config = load_config()
    python_cmd = config.get("python_cmd", sys.executable)
    # Split in case of "py -3.14" style commands
    cmd_parts = python_cmd.split() if isinstance(python_cmd, str) else [python_cmd]

    result = subprocess.run(
        [*cmd_parts, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"notebooklm subprocess failed (exit {result.returncode}):\n{stderr}"
        )

    return result.stdout
