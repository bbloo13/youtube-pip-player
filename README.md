# youtube-pip-player

브라우저 탭 여러 개 대신 쓰는 가벼운 유튜브 PIP 플레이어. 영상은 `mpv`(네이티브 플레이어)로 재생하고 주소는 `yt-dlp`로 가져와서, 브라우저보다 램을 훨씬 적게 씁니다(재생 중 150MB 안팎, 대기 중 약 25MB). 목록을 고르는 창은 [youtube-music-widget](https://github.com/bbloo13/youtube-music-widget)과 같은 다크 위젯 디자인입니다.

## 기능

- **내 목록**: 영상·재생목록·채널 링크나 검색어를 넣어 직접 담는 목록. 행마다 ✕로 지우기, 순서 옮기기, 전체/선택부터 재생
- **추천**: 로그인한 계정의 유튜브 홈 추천. 행의 `+`는 내 목록에 담고(추천에서는 사라짐), `✕`는 이 앱 안에서 치움(유튜브 계정의 "관심 없음"으로는 전달되지 않음)
- **구독**: 구독한 채널의 최신 영상만. 행의 `+`로 내 목록에 담고, `✕`로 이 앱에서 치움
- **재생목록**: 내 계정의 재생목록. 더블클릭하면 안으로 들어가 영상을 고를 수 있음
- 내 목록에서 ✕/삭제로 뺀 영상은 추천·구독에도 다시 뜨지 않음 (앱 안에서만 적용, 유튜브 계정에는 전달되지 않음)
- **mpv 창 위치·크기 기억**: 옮겨 두면 다음 영상도 그 자리에서 열림 (처음엔 화면 왼쪽 위)
- **썸네일 + 올린 사람 이름**이 보이는 목록 (둥근 폰트 Gowun Dodum)
- **바로 열리기**: mpv를 대기 상태로 미리 켜 두고, 목록의 영상 주소를 뒤에서 미리 받아 둠(앱을 껐다 켜도 1.5시간 동안 유지). 미리 받아 둔 영상은 0.3~1초 안에 시작
- **같은 창에서 화질 변경** (360/480/720/1080p): 창을 닫지 않고 보던 위치에서 이어짐
- **램 게이지**: 영상 / 앱 사용량을 막대로 표시
- **커스텀 플레이어 UI**: 제목줄(끌어서 이동, ✕), 둥근 진행바, 이전/재생/다음, 볼륨. 마우스를 창 밖으로 빼면 바로 숨음
- **크기 조절**: 창 가장자리(바깥 4px · 안쪽 12px, 모서리는 더 넓게)에 눈에 안 보이는 조절 띠를 얹어 마우스 모양이 바뀌는 범위를 넓혔고, 끌면 영상 비율을 유지한 채 크기가 바뀜. 최소 크기(360×200, 화면 배율에 맞춰 커짐)보다 작아지지 않음
- 캐시 + 5분마다 자동 새로고침, 계정은 앱 전용 Edge 프로필로 로그인(`로그인` 버튼). 로그인하면 앱이 알아서 감지해서 창을 닫고 목록을 불러옴(⟳ 불필요)

## 준비물 (Windows)

- Python 3.12 + `pip install -r requirements.txt` (Pillow, websocket-client)
- [mpv](https://mpv.io) — `C:\Program Files\MPV Player\mpv.exe` 에 설치 (`app.py` 의 `MPV` 에서 경로 변경 가능)
- `yt-dlp`, `ffmpeg`, `deno` (모두 PATH 에 있어야 함, 예: `winget install yt-dlp.yt-dlp`)
- Microsoft Edge (계정 연동용)

## 실행

```bash
git clone https://github.com/bbloo13/youtube-pip-player.git
cd youtube-pip-player
pip install -r requirements.txt
pythonw app.py     # 또는 app.bat
```

계정 목록(추천·재생목록)을 보려면 앱에서 `로그인`을 눌러 열린 Edge 창에서 구글에 로그인하세요. 로그인이 끝나면 앱이 Edge의 디버그 포트로 쿠키를 지켜보다가 스스로 감지해 창을 닫습니다. 이미 로그인된 프로필이 있으면 앱을 켤 때 자동으로 연동 처리하고, `websocket-client` 가 없으면 예전처럼 로그인 후 ⟳ 를 눌러야 합니다. 로그인 정보는 이 폴더가 아니라 `%LOCALAPPDATA%\youtube-pip-player` 에만 저장됩니다. 직접 담은 목록(`playlist.json`)도 개인 데이터라 저장소에는 올리지 않습니다.

## 파일

- `app.py` — 목록 앱 (tkinter) 과 mpv 제어
- `resize_grips.py` — mpv 창 둘레의 크기 조절 띠 (Win32, 최소 크기 · 비율 유지)
- `assets/mpv/yt-pip-osc.lua` — mpv 에 얹는 컨트롤바/제목줄
- `assets/fonts/GowunDodum-Regular.ttf` — 목록 폰트 ([Gowun Dodum](https://fonts.google.com/specimen/Gowun+Dodum), SIL Open Font License 1.1)

## 버전

날짜를 그대로 씁니다 (예: `20261108`, 같은 날 또 올리면 `20261108.1`, `20261108.2`). `app.py` 의 `VERSION`, 커밋 메시지 `v20261108: ...`, git 태그 `20261108` 이 같은 값입니다.
