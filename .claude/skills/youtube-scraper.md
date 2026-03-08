# YouTube Scraper Skill

YouTube 데이터를 수집하는 커스텀 스킬. yt-dlp 기반.

## Trigger
사용자가 YouTube 영상 정보 수집, 다운로드, 자막 추출, 채널/플레이리스트 스크래핑을 요청할 때 이 스킬을 사용합니다.

## 사용 가능한 모듈

### 1. 메타데이터 수집 (`YouTubeScraper`)
```python
from youtube_scraper import YouTubeScraper

scraper = YouTubeScraper()

# 단일 영상 정보
info = scraper.get_video_info("https://youtube.com/watch?v=...")

# 상세 정보 (포맷, 자막 목록 포함)
detailed = scraper.get_detailed_info("https://youtube.com/watch?v=...")

# 채널 전체 영상
videos = scraper.get_channel_info("https://youtube.com/@channel")

# 플레이리스트
videos = scraper.get_playlist_info("https://youtube.com/playlist?list=...")

# 검색
results = scraper.search("키워드", max_results=10)
```

### 2. 다운로드 (`VideoDownloader`)
```python
from youtube_scraper import VideoDownloader

dl = VideoDownloader(output_dir="output/downloads")

# 영상 다운로드 (best, 1080p, 720p, 480p, 360p, worst)
dl.download_video(url, quality="720p")

# 오디오만 (mp3, wav, aac, flac, m4a)
dl.download_audio(url, audio_format="mp3")

# 플레이리스트 다운로드
dl.download_playlist(url, quality="720p", video_range=(1, 5))
```

### 3. 자막 추출 (`SubtitleExtractor`)
```python
from youtube_scraper import SubtitleExtractor

sub = SubtitleExtractor(output_dir="output/subtitles")

# 자막 목록 확인
available = sub.list_subtitles(url)

# 자막 다운로드 (srt, vtt, json3, txt)
files = sub.download_subtitles(url, languages=["ko", "en"], fmt="srt")

# 텍스트만 추출
text = sub.extract_text(url, language="ko")
```

### 4. 데이터 내보내기 (`DataExporter`)
```python
from youtube_scraper import DataExporter

exporter = DataExporter(output_dir="output")

# JSON 저장
exporter.to_json(data, "result.json")

# CSV 저장
exporter.to_csv(data, "result.csv")

# 둘 다
exporter.export_both(data, "result")
```

## CLI 사용법

```bash
# 영상 정보
python -m youtube_scraper.cli info "URL" --format both
python -m youtube_scraper.cli info "URL" --detailed

# 검색
python -m youtube_scraper.cli search "키워드" -n 20

# 채널/플레이리스트
python -m youtube_scraper.cli channel "URL"
python -m youtube_scraper.cli playlist "URL"

# 다운로드
python -m youtube_scraper.cli download "URL" -q 720p
python -m youtube_scraper.cli download "URL" --audio-only --audio-format mp3

# 자막
python -m youtube_scraper.cli subtitles "URL" --list
python -m youtube_scraper.cli subtitles "URL" -l ko en
python -m youtube_scraper.cli subtitles "URL" --text-only -l ko
```

## 참고사항
- 쿠키 파일: `--cookies cookies.txt` 옵션으로 인증 필요한 영상 접근
- 출력 디렉토리: `--output-dir` 또는 `-o` 옵션 (기본: `output/`)
- CSV는 UTF-8 BOM 포함 (엑셀 호환)
