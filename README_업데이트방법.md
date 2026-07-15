# 업데이트 내용

1. 기존 5분 뉴스의 영어기사 한글 요약을 최대 2줄에서 최대 4줄로 확대합니다.
2. 미국장 마감 뒤 한국시간 오전 7시에 미국장 요약을 한 번 보냅니다.
3. 나스닥·다우·S&P500·필라델피아 반도체, 금리·환율·유가·VIX, 특이 급등락, 주요 발언·뉴스, 경제지표·실적일정, 다음 국내장 체크포인트를 포함합니다.

# GitHub에 올릴 파일

기존 파일 교체:
- market_news_alert.py
- requirements.txt

새로 추가:
- morning_us_market_report.py
- .github/workflows/morning-us-market-report.yml

텔레그램 Secret은 기존 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID를 그대로 사용합니다.
