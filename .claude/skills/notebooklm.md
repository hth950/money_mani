# NotebookLM Skill

Google NotebookLM을 프로그래밍 방식으로 제어하는 커스텀 스킬. notebooklm-py 기반.

## Trigger
사용자가 NotebookLM 노트북 생성, 소스 추가, 질문, 오디오/비디오/퀴즈/슬라이드 등 콘텐츠 생성을 요청할 때 이 스킬을 사용합니다.

## 실행 환경
- Python: `py -3.14` 사용 필수 (3.8에는 미설치)
- 인증: `notebooklm login` 완료 상태

## CLI 명령어

### 노트북 관리
```bash
notebooklm create "노트북 이름"
notebooklm use <notebook_id>
```

### 소스 추가
```bash
notebooklm source add "https://example.com"
notebooklm source add "./paper.pdf"
```

### 질문하기
```bash
notebooklm ask "핵심 주제가 뭐야?"
```

### 콘텐츠 생성
```bash
notebooklm generate audio "재밌게 만들어줘" --wait
notebooklm generate video --style whiteboard --wait
notebooklm generate quiz --difficulty hard
notebooklm generate flashcards --quantity more
notebooklm generate slide-deck
notebooklm generate infographic --orientation portrait
notebooklm generate mind-map
notebooklm generate data-table "핵심 개념 비교"
```

### 다운로드
```bash
notebooklm download audio ./podcast.mp3
notebooklm download video ./overview.mp4
notebooklm download quiz --format markdown ./quiz.md
notebooklm download flashcards --format json ./cards.json
notebooklm download slide-deck ./slides.pdf
notebooklm download mind-map ./mindmap.json
notebooklm download data-table ./data.csv
```

## Python API

### 기본 패턴 (async 필수)
```python
import asyncio
from notebooklm import NotebookLMClient

async def main():
    async with await NotebookLMClient.from_storage() as client:
        # 노트북 목록
        notebooks = await client.notebooks.list()

        # 노트북 생성
        nb = await client.notebooks.create("Research")

        # 소스 추가
        await client.sources.add_url(nb.id, "https://example.com", wait=True)

        # 질문
        result = await client.chat.ask(nb.id, "요약해줘")
        print(result.answer)

        # 오디오 생성 + 다운로드
        status = await client.artifacts.generate_audio(nb.id, instructions="재밌게")
        await client.artifacts.wait_for_completion(nb.id, status.task_id)
        await client.artifacts.download_audio(nb.id, "podcast.mp3")

        # 퀴즈 생성 + JSON 다운로드
        status = await client.artifacts.generate_quiz(nb.id)
        await client.artifacts.wait_for_completion(nb.id, status.task_id)
        await client.artifacts.download_quiz(nb.id, "quiz.json", output_format="json")

        # 마인드맵
        await client.artifacts.generate_mind_map(nb.id)
        await client.artifacts.download_mind_map(nb.id, "mindmap.json")

        # 슬라이드
        await client.artifacts.generate_slide_deck(nb.id)
        await client.artifacts.download_slide_deck(nb.id, "slides.pdf")

asyncio.run(main())
```

## 지원 소스 형식
URL, YouTube, PDF, 텍스트, Markdown, Word, 오디오, 비디오, 이미지, Google Drive, 붙여넣기 텍스트

## 생성 가능 콘텐츠
오디오(50+ 언어), 비디오(9 스타일), 슬라이드, 인포그래픽, 퀴즈, 플래시카드, 리포트, 데이터 테이블, 마인드맵

## 참고사항
- 비공식 API이므로 Google 변경 시 동작 불가 가능
- Rate limit 존재 — 대량 요청 시 간격 필요
- `--wait` 플래그로 생성 완료까지 대기
