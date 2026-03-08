"""YouTube metadata scraper using yt-dlp."""

import yt_dlp


class YouTubeScraper:
    """Extracts metadata from YouTube videos, channels, playlists, and search results."""

    def __init__(self, cookies_file=None):
        self._base_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "ignoreerrors": True,
            "ignore_no_formats_error": True,
        }
        if cookies_file:
            self._base_opts["cookiefile"] = cookies_file

    def _extract(self, url, opts=None):
        merged = {**self._base_opts, **(opts or {})}
        with yt_dlp.YoutubeDL(merged) as ydl:
            return ydl.extract_info(url, download=False)

    def get_video_info(self, url):
        """Extract metadata for a single video."""
        info = self._extract(url)
        if not info:
            return None
        return self._parse_video(info)

    def get_playlist_info(self, url):
        """Extract metadata for all videos in a playlist."""
        info = self._extract(url, {"extract_flat": "in_playlist"})
        if not info:
            return []
        entries = info.get("entries") or []
        playlist_meta = {
            "playlist_title": info.get("title"),
            "playlist_id": info.get("id"),
            "playlist_url": info.get("webpage_url"),
            "video_count": len(entries),
        }
        videos = []
        for entry in entries:
            if entry:
                video = self._parse_flat_entry(entry)
                video["playlist"] = playlist_meta
                videos.append(video)
        return videos

    def get_channel_info(self, url):
        """Extract metadata for all videos on a channel."""
        info = self._extract(url, {"extract_flat": "in_playlist"})
        if not info:
            return []
        entries = info.get("entries") or []
        channel_meta = {
            "channel_name": info.get("channel") or info.get("uploader"),
            "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url") or info.get("webpage_url"),
        }
        videos = []
        for entry in entries:
            if entry:
                video = self._parse_flat_entry(entry)
                video["channel"] = channel_meta
                videos.append(video)
        return videos

    def search(self, query, max_results=10):
        """Search YouTube and return metadata for matching videos."""
        search_url = f"ytsearch{max_results}:{query}"
        info = self._extract(search_url)
        if not info:
            return []
        entries = info.get("entries") or []
        return [self._parse_video(e) for e in entries if e]

    def get_detailed_info(self, url):
        """Extract full detailed metadata for a single video (slower but comprehensive)."""
        info = self._extract(url)
        if not info:
            return None
        return {
            **self._parse_video(info),
            "formats": [
                {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution"),
                    "fps": f.get("fps"),
                    "filesize": f.get("filesize"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                }
                for f in (info.get("formats") or [])
            ],
            "subtitles_available": list((info.get("subtitles") or {}).keys()),
            "auto_subtitles_available": list(
                (info.get("automatic_captions") or {}).keys()
            ),
            "chapters": info.get("chapters"),
            "comments": info.get("comments"),
        }

    def _parse_video(self, info):
        return {
            "id": info.get("id"),
            "title": info.get("title"),
            "url": info.get("webpage_url"),
            "description": info.get("description"),
            "duration": info.get("duration"),
            "duration_string": info.get("duration_string"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "upload_date": info.get("upload_date"),
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url"),
            "thumbnail": info.get("thumbnail"),
            "tags": info.get("tags"),
            "categories": info.get("categories"),
            "age_limit": info.get("age_limit"),
            "language": info.get("language"),
        }

    def _parse_flat_entry(self, entry):
        return {
            "id": entry.get("id"),
            "title": entry.get("title"),
            "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "channel": entry.get("channel") or entry.get("uploader"),
        }
