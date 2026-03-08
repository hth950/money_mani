"""NotebookLMAnalyzer: manages notebooks and extracts strategies via NotebookLM."""

from notebooklm_analyzer.client import run_notebooklm_command
from notebooklm_analyzer.prompts import STRATEGY_EXTRACTION_PROMPT, VALIDATION_PROMPT


class NotebookLMAnalyzer:
    """High-level interface for NotebookLM research sessions.

    Each method builds a self-contained Python 3.14 script and executes it
    via run_notebooklm_command() to avoid Python version conflicts.
    """

    def create_research_session(self, name: str) -> str:
        """Create a new NotebookLM notebook and return its notebook_id.

        Args:
            name: Human-readable name for the research session.

        Returns:
            notebook_id string from NotebookLM.
        """
        script = f"""
import asyncio
from notebooklm import NotebookLMClient

async def main():
    client = await NotebookLMClient.from_storage()
    notebook = await client.create_notebook(title={name!r})
    print(notebook.id)

asyncio.run(main())
"""
        output = run_notebooklm_command(script)
        return output.strip()

    def add_videos(self, notebook_id: str, video_urls: list[str]) -> None:
        """Add YouTube video URLs as sources to a notebook.

        Args:
            notebook_id: Notebook identifier returned by create_research_session.
            video_urls: List of YouTube URLs to add as sources.
        """
        urls_repr = repr(video_urls)
        script = f"""
import asyncio
from notebooklm import NotebookLMClient

async def main():
    client = await NotebookLMClient.from_storage()
    notebook = await client.get_notebook({notebook_id!r})
    for url in {urls_repr}:
        await notebook.add_source(url=url)
        print(f"Added: {{url}}")

asyncio.run(main())
"""
        run_notebooklm_command(script)

    def extract_strategies(self, notebook_id: str) -> str:
        """Ask NotebookLM to extract investment strategies from notebook sources.

        Args:
            notebook_id: Notebook identifier.

        Returns:
            Raw strategy analysis text from NotebookLM.
        """
        prompt = STRATEGY_EXTRACTION_PROMPT.replace('"', '\\"')
        script = f"""
import asyncio
from notebooklm import NotebookLMClient

async def main():
    client = await NotebookLMClient.from_storage()
    notebook = await client.get_notebook({notebook_id!r})
    response = await notebook.chat("{prompt}")
    print(response)

asyncio.run(main())
"""
        return run_notebooklm_command(script).strip()

    def get_summary(self, notebook_id: str) -> str:
        """Get a general summary of all sources in the notebook.

        Args:
            notebook_id: Notebook identifier.

        Returns:
            Summary text from NotebookLM.
        """
        script = f"""
import asyncio
from notebooklm import NotebookLMClient

async def main():
    client = await NotebookLMClient.from_storage()
    notebook = await client.get_notebook({notebook_id!r})
    response = await notebook.chat("이 노트북의 모든 소스를 요약해주세요. 핵심 투자 개념과 전략을 포함해주세요.")
    print(response)

asyncio.run(main())
"""
        return run_notebooklm_command(script).strip()
