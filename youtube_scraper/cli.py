"""CLI interface for YouTube scraper."""

import argparse
import sys

from .scraper import YouTubeScraper
from .downloader import VideoDownloader
from .subtitles import SubtitleExtractor
from .exporter import DataExporter


def main():
    parser = argparse.ArgumentParser(description="YouTube Scraper powered by yt-dlp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Common args
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cookies", help="Path to cookies file")
    common.add_argument("--output-dir", "-o", default="output", help="Output directory")

    # --- info ---
    info_p = subparsers.add_parser("info", parents=[common], help="Get video metadata")
    info_p.add_argument("url", help="YouTube URL")
    info_p.add_argument("--detailed", action="store_true", help="Include format/subtitle details")
    info_p.add_argument("--format", choices=["json", "csv", "both"], default="both")

    # --- search ---
    search_p = subparsers.add_parser("search", parents=[common], help="Search YouTube")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--max-results", "-n", type=int, default=10)
    search_p.add_argument("--format", choices=["json", "csv", "both"], default="both")

    # --- channel ---
    chan_p = subparsers.add_parser("channel", parents=[common], help="Scrape channel metadata")
    chan_p.add_argument("url", help="Channel URL")
    chan_p.add_argument("--format", choices=["json", "csv", "both"], default="both")

    # --- playlist ---
    pl_p = subparsers.add_parser("playlist", parents=[common], help="Scrape playlist metadata")
    pl_p.add_argument("url", help="Playlist URL")
    pl_p.add_argument("--format", choices=["json", "csv", "both"], default="both")

    # --- download ---
    dl_p = subparsers.add_parser("download", parents=[common], help="Download video/audio")
    dl_p.add_argument("url", help="YouTube URL")
    dl_p.add_argument("--quality", "-q", default="best",
                       choices=["best", "1080p", "720p", "480p", "360p", "worst"])
    dl_p.add_argument("--audio-only", action="store_true", help="Download audio only")
    dl_p.add_argument("--audio-format", default="mp3",
                       choices=["mp3", "wav", "aac", "flac", "m4a"])

    # --- subtitles ---
    sub_p = subparsers.add_parser("subtitles", parents=[common], help="Extract subtitles")
    sub_p.add_argument("url", help="YouTube URL")
    sub_p.add_argument("--languages", "-l", nargs="+", default=None,
                        help="Language codes (e.g., ko en)")
    sub_p.add_argument("--list", action="store_true", help="List available subtitles only")
    sub_p.add_argument("--text-only", action="store_true", help="Extract text content only")
    sub_p.add_argument("--sub-format", default="srt", choices=["srt", "vtt", "json3", "txt"])
    sub_p.add_argument("--no-auto", action="store_true", help="Skip auto-generated captions")

    args = parser.parse_args()
    cookies = getattr(args, "cookies", None)
    output_dir = getattr(args, "output_dir", "output")

    if args.command == "info":
        scraper = YouTubeScraper(cookies_file=cookies)
        if args.detailed:
            data = scraper.get_detailed_info(args.url)
        else:
            data = scraper.get_video_info(args.url)
        if not data:
            print("Failed to extract video info.", file=sys.stderr)
            sys.exit(1)
        _export(data, args.format, output_dir, "video_info")

    elif args.command == "search":
        scraper = YouTubeScraper(cookies_file=cookies)
        data = scraper.search(args.query, max_results=args.max_results)
        print(f"Found {len(data)} results.")
        _export(data, args.format, output_dir, f"search_{args.query[:30].replace(' ', '_')}")

    elif args.command == "channel":
        scraper = YouTubeScraper(cookies_file=cookies)
        data = scraper.get_channel_info(args.url)
        print(f"Found {len(data)} videos.")
        _export(data, args.format, output_dir, "channel_videos")

    elif args.command == "playlist":
        scraper = YouTubeScraper(cookies_file=cookies)
        data = scraper.get_playlist_info(args.url)
        print(f"Found {len(data)} videos.")
        _export(data, args.format, output_dir, "playlist_videos")

    elif args.command == "download":
        dl = VideoDownloader(output_dir=f"{output_dir}/downloads", cookies_file=cookies)
        if args.audio_only:
            result = dl.download_audio(args.url, audio_format=args.audio_format)
        else:
            result = dl.download_video(args.url, quality=args.quality)
        print(f"Success: {len(result['success'])} | Failed: {len(result['failed'])}")

    elif args.command == "subtitles":
        sub = SubtitleExtractor(output_dir=f"{output_dir}/subtitles", cookies_file=cookies)
        if args.list:
            available = sub.list_subtitles(args.url)
            print(f"Manual: {', '.join(available['manual']) or 'none'}")
            print(f"Auto:   {', '.join(available['auto']) or 'none'}")
        elif args.text_only:
            lang = args.languages[0] if args.languages else "ko"
            text = sub.extract_text(args.url, language=lang, auto_generated=not args.no_auto)
            if text:
                print(text)
            else:
                print("No subtitles found.", file=sys.stderr)
                sys.exit(1)
        else:
            files = sub.download_subtitles(
                args.url,
                languages=args.languages,
                auto_generated=not args.no_auto,
                fmt=args.sub_format,
            )
            print(f"Downloaded {len(files)} subtitle file(s).")


def _export(data, fmt, output_dir, base_name):
    exporter = DataExporter(output_dir=output_dir)
    if fmt == "json":
        path = exporter.to_json(data, f"{base_name}.json")
        print(f"Saved: {path}")
    elif fmt == "csv":
        path = exporter.to_csv(data, f"{base_name}.csv")
        print(f"Saved: {path}")
    else:
        paths = exporter.export_both(data, base_name)
        print(f"Saved: {paths['json']}")
        print(f"Saved: {paths['csv']}")


if __name__ == "__main__":
    main()
