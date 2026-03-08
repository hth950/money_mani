"""YouTube video/audio downloader using yt-dlp."""

import os
import yt_dlp


class VideoDownloader:
    """Downloads videos and audio from YouTube."""

    def __init__(self, output_dir="output/downloads", cookies_file=None):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self._cookies_file = cookies_file

    def _get_opts(self, extra=None):
        opts = {
            "outtmpl": os.path.join(self.output_dir, "%(title)s [%(id)s].%(ext)s"),
            "ignoreerrors": True,
            "no_warnings": True,
            "ignore_no_formats_error": True,
        }
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        if extra:
            opts.update(extra)
        return opts

    def download_video(self, url, quality="best"):
        """Download video with specified quality.

        Args:
            url: YouTube video URL.
            quality: 'best', '1080p', '720p', '480p', '360p', or 'worst'.
        """
        format_map = {
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
            "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
            "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
            "360p": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
            "worst": "worstvideo+worstaudio/worst",
        }
        fmt = format_map.get(quality, format_map["best"])
        opts = self._get_opts({"format": fmt, "merge_output_format": "mp4"})
        return self._download(url, opts)

    def download_audio(self, url, audio_format="mp3"):
        """Download audio only.

        Args:
            url: YouTube video URL.
            audio_format: 'mp3', 'wav', 'aac', 'flac', 'm4a'.
        """
        opts = self._get_opts({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "192",
            }],
        })
        return self._download(url, opts)

    def download_playlist(self, url, quality="best", video_range=None):
        """Download all videos from a playlist.

        Args:
            url: Playlist URL.
            quality: Video quality preset.
            video_range: Optional tuple (start, end) for partial download.
        """
        format_map = {
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
        }
        fmt = format_map.get(quality, format_map["best"])
        extra = {
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(
                self.output_dir, "%(playlist_title)s", "%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"
            ),
        }
        if video_range:
            extra["playliststart"] = video_range[0]
            extra["playlistend"] = video_range[1]
        opts = self._get_opts(extra)
        return self._download(url, opts)

    def _download(self, url, opts):
        results = {"success": [], "failed": []}
        original_hook = opts.get("progress_hooks", [])

        def hook(d):
            if d["status"] == "finished":
                results["success"].append(d.get("filename", "unknown"))

        opts["progress_hooks"] = original_hook + [hook]

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                ydl.download([url])
            except yt_dlp.utils.DownloadError as e:
                results["failed"].append(str(e))

        return results
