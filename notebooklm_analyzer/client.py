"""Subprocess wrapper to call notebooklm-py under Python 3.14."""

import subprocess
import sys
import textwrap


def run_notebooklm_command(script: str) -> str:
    """Execute a self-contained async Python script via py -3.14 and return stdout.

    notebooklm-py is installed on Python 3.14, not in the project venv.
    This wrapper runs the script in a subprocess and captures output.

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

    result = subprocess.run(
        ["py", "-3.14", "-c", script],
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
