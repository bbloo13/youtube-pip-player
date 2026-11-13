@echo off
rem list.txt 에 유튜브 링크를 한 줄씩 적고 이 파일을 실행하세요.
rem 키: > 다음 영상, < 이전 영상, 스페이스 일시정지, q 종료
"C:\Program Files\MPV Player\mpv.exe" --ontop --autofit=30%% --geometry=100%%:100%% --ytdl-format="bestvideo[height<=720]+bestaudio/best[height<=720]" --playlist="%~dp0list.txt"
