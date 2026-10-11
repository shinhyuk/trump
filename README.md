# 트럼프 따라사기

트럼프 대통령이 미 정부윤리청(OGE)에 제출하는 **Form 278-T(정기 거래 보고서)** 를 매일 확인해서,
새로 공개된 매매 내역과 따라 살 종목 목록을 보여주는 페이지입니다.

## 동작 방식

1. GitHub Actions가 매일 한국시간 오전 8시 17분에 `scripts/update.py`를 실행합니다.
2. OGE 공시 검색 API에서 트럼프의 278-T 목록을 받아, 처음 보는 PDF만 내려받아 파싱합니다.
   원본이 스캔본이라 텍스트 레이어 위치로 행을 맞추고, 텍스트가 없는 페이지는 Tesseract로 OCR합니다.
3. 종목명을 나스닥 심볼 목록과 맞춰 티커를 붙이고, Yahoo Finance에서 전일 종가를 가져옵니다.
4. `docs/data/summary.json`을 갱신해 커밋하고, `docs/`를 GitHub Pages로 배포합니다.

## 페이지 구성

- **지금 따라 살 종목**: 최신 공시(또는 최근 3건, 전체 누적)의 순매수 추정액 비율대로 투자금을 나눈 목록과 주수
- **최신 공시 변동**: 신규 편입, 추가 매수, 순매도 종목
- **최신 공시 거래 전체**: 검색·필터 가능한 원장
- **누적 순매수 상위**: 공시 합산으로 추정한 보유 종목
- **공시 기록**: 공시별 거래 건수와 원문 PDF 링크

## 처음 설정

저장소 **Settings → Pages → Build and deployment → Source** 를 **GitHub Actions** 로 바꾼 뒤,
**Actions → Daily update → Run workflow** 를 한 번 실행하세요.

## 로컬 실행

```bash
sudo apt-get install tesseract-ocr
pip install -r requirements.txt
python scripts/update.py
python -m http.server -d docs 8000
```

## 한계

- 278-T는 거래 후 최대 45일 뒤 공개됩니다.
- 금액은 구간으로만 신고되며, 여기서는 구간 중간값으로 추정합니다.
- OCR 오류로 일부 종목명·날짜·구분이 틀릴 수 있습니다. 원문 PDF를 함께 확인하세요.
- 투자 권유가 아닙니다.
