"""YouTube subtitle/caption extractor using yt-dlp."""

import os
import yt_dlp


class SubtitleExtractor:
    """Extracts subtitles and captions from YouTube videos."""

    def __init__(self, output_dir="output/subtitles", cookies_file=None):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self._cookies_file = cookies_file

    def list_subtitles(self, url):
        """List all available subtitles for a video."""
        opts = {"quiet": True, "no_warnings": True, "ignore_no_formats_error": True}
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return {"manual": [], "auto": []}

        manual = list((info.get("subtitles") or {}).keys())
        auto = list((info.get("automatic_captions") or {}).keys())
        return {"manual": manual, "auto": auto}

    def download_subtitles(self, url, languages=None, auto_generated=True, fmt="srt"):
        """Download subtitles for a video.

        Args:
            url: YouTube video URL.
            languages: List of language codes (e.g., ['ko', 'en']). None for all.
            auto_generated: Include auto-generated captions.
            fmt: Subtitle format - 'srt', 'vtt', 'json3', 'txt'.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignore_no_formats_error": True,
            "writesubtitles": True,
            "subtitlesformat": fmt,
            "outtmpl": os.path.join(self.output_dir, "%(title)s [%(id)s].%(ext)s"),
            "skip_download": True,
        }
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        if auto_generated:
            opts["writeautomaticsub"] = True
        if languages:
            opts["subtitleslangs"] = languages
        else:
            opts["subtitleslangs"] = ["all"]

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        return self._find_subtitle_files(url)

    def extract_text(self, url, language="ko", auto_generated=True):
        """Extract subtitle text content as a string.

        Args:
            url: YouTube video URL.
            language: Language code.
            auto_generated: Include auto-generated captions.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignore_no_formats_error": True,
            "writesubtitles": True,
            "subtitlesformat": "json3",
            "skip_download": True,
        }
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        if auto_generated:
            opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = [language]

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

        subs = info.get("subtitles", {})
        auto_subs = info.get("automatic_captions", {})

        sub_data = subs.get(language) or (auto_subs.get(language) if auto_generated else None)
        if not sub_data:
            return None

        # Get json3 format for structured text
        json3_sub = next((s for s in sub_data if s.get("ext") == "json3"), None)
        if json3_sub and "url" in json3_sub:
            import urllib.request
            import json

            with urllib.request.urlopen(json3_sub["url"]) as resp:
                data = json.loads(resp.read())
            events = data.get("events", [])
            lines = []
            for event in events:
                segs = event.get("segs", [])
                text = "".join(s.get("utf8", "") for s in segs).strip()
                if text and text != "\n":
                    lines.append(text)
            return "\n".join(lines)

        # Fallback: get vtt
        vtt_sub = next((s for s in sub_data if s.get("ext") == "vtt"), None)
        if vtt_sub and "url" in vtt_sub:
            import urllib.request

            with urllib.request.urlopen(vtt_sub["url"]) as resp:
                return resp.read().decode("utf-8")

        return None

    def _find_subtitle_files(self, url):
        files = []
        for f in os.listdir(self.output_dir):
            if f.endswith((".srt", ".vtt", ".json3", ".txt")):
                files.append(os.path.join(self.output_dir, f))
        return files
