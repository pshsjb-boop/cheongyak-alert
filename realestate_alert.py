"""
서초·강남·송파·용산 아파트 실거래가 + 토지거래허가구역(토허제) 뉴스 텔레그램 알림
- 실거래가: 국토교통부_아파트 매매 실거래가 자료 (공공데이터포털, DATA_GO_KR_KEY 공용)
- 토허제: 구글 뉴스 RSS 검색

환경변수: DATA_GO_KR_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN(선택)
"""
import html
import json
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from cheongyak_alert import send_telegram

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).parent
TRADE_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
GU = {"11680": "강남구", "11650": "서초구", "11710": "송파구", "11170": "용산구"}
HISTORY_MONTHS = 12        # 최근 1년 최고가 비교 기간
MAX_LINES_PER_GU = 25      # 구별 최대 표시 건수
NEWS_QUERY = "토지거래허가구역 (강남 OR 서초 OR 송파 OR 용산 OR 서울)"


def load(name):
    p = HERE / name
    return set(json.loads(p.read_text())) if p.exists() else None


def save(name, s):
    (HERE / name).write_text(json.dumps(sorted(s), ensure_ascii=False))


def months_back(today, n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def fetch_trades(key, lawd, ymd):
    rows, page = [], 1
    while True:
        r = requests.get(TRADE_URL, params={
            "serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ymd,
            "pageNo": page, "numOfRows": 1000,
        }, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        code = root.findtext(".//resultCode")
        if code not in (None, "00", "000"):
            raise RuntimeError(f"{root.findtext('.//resultMsg')} ({code})")
        items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
        rows += items
        total = int(root.findtext(".//totalCount") or 0)
        if not items or len(rows) >= total:
            return rows
        page += 1


def price(t):
    return int(t.get("dealAmount", "0").replace(",", "") or 0)  # 만원


def area_key(t):
    try:
        return int(float(t.get("excluUseAr") or 0))
    except ValueError:
        return 0


def trade_id(t):
    return "|".join(t.get(k, "") for k in (
        "aptSeq", "aptNm", "dealYear", "dealMonth", "dealDay", "floor", "excluUseAr", "dealAmount", "cdealType"))


def eok(manwon):
    e, r = divmod(manwon, 10000)
    return f"{e}억 {r:,}" if e and r else (f"{e}억" if e else f"{r:,}만")


def trade_section(key, today):
    seen = load("seen_trades.json")
    first_run = seen is None
    seen = seen or set()
    months = months_back(today, HISTORY_MONTHS)
    blocks, new_seen, total_new = [], set(seen), 0

    for lawd, gu in GU.items():
        all_rows = []
        for i, ym in enumerate(months):
            try:
                all_rows += fetch_trades(key, lawd, ym)
            except Exception as e:
                if i < 2:  # 최근 2개월 조회 실패만 경고
                    print(f"[경고] {gu} {ym} 실거래 조회 실패: {e}", file=sys.stderr)
        valid = [t for t in all_rows if t.get("cdealType", "") != "O"]
        recent_ids = {trade_id(t) for t in all_rows if f"{t.get('dealYear')}{int(t.get('dealMonth') or 0):02d}" in months[:2]}
        new = [t for t in all_rows if trade_id(t) in recent_ids and trade_id(t) not in seen]
        new_seen |= recent_ids
        if first_run or not new:
            continue

        lines = []
        for t in sorted(new, key=price, reverse=True):
            tid = trade_id(t)
            if t.get("cdealType") == "O":
                mark = "❌해제 "
            else:
                peers = [price(p) for p in valid if p.get("aptSeq") == t.get("aptSeq")
                         and area_key(p) == area_key(t) and trade_id(p) != tid]
                mark = "🔺신고가 " if peers and price(t) > max(peers) else ""
            d = f"{int(t.get('dealMonth') or 0)}/{int(t.get('dealDay') or 0)}"
            lines.append(f"• {mark}{html.escape(t.get('aptNm', ''))} {area_key(t)}㎡ {t.get('floor', '')}층 "
                         f"<b>{eok(price(t))}</b> ({html.escape(t.get('umdNm', ''))}, {d} 계약)")
        total_new += len(lines)
        more = f"\n  …외 {len(lines) - MAX_LINES_PER_GU}건" if len(lines) > MAX_LINES_PER_GU else ""
        blocks.append(f"📍 <b>{gu}</b> {len(lines)}건\n" + "\n".join(lines[:MAX_LINES_PER_GU]) + more)

    save("seen_trades.json", new_seen)
    if first_run:
        return "🏢 <b>아파트 실거래가 알림 시작</b>\n강남·서초·송파·용산의 새로 신고된 매매 거래를 내일부터 매일 알려 드려요."
    if not blocks:
        return "🏢 <b>새로 신고된 아파트 매매</b>\n• 없음 (강남·서초·송파·용산)"
    return (f"🏢 <b>새로 신고된 아파트 매매 {total_new}건</b>\n"
            "🔺신고가 = 같은 단지·같은 면적 최근 1년 최고가 경신\n\n" + "\n\n".join(blocks))


def news_section(today):
    seen = load("seen_news.json")
    first_run = seen is None
    seen = seen or set()
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": NEWS_QUERY + " when:3d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    lines, titles = [], set()
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        base = title.rsplit(" - ", 1)[0]
        if "토지거래허가" not in title and "토허" not in title:
            continue
        if base in seen or base in titles:
            continue
        titles.add(base)
        try:
            when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST)
            when_s = f"{when.month}/{when.day}"
        except Exception:
            when_s = ""
        lines.append(f'• <a href="{html.escape(link)}">{html.escape(title)}</a> {when_s}')
    save("seen_news.json", seen | titles)
    if not lines:
        return "📰 <b>토지거래허가구역 소식</b>\n• 새 소식 없음"
    head = "📰 <b>토지거래허가구역 소식</b>" + (" (최근 3일)" if first_run else "")
    return head + "\n" + "\n".join(lines[:15])


def main():
    key = os.environ["DATA_GO_KR_KEY"]
    today = datetime.now(KST).date()
    parts = []
    for fn in (lambda: trade_section(key, today), lambda: news_section(today)):
        try:
            parts.append(fn())
        except Exception as e:
            print(f"[오류] {e}", file=sys.stderr)
            parts.append(f"⚠️ 일부 정보를 불러오지 못했어요: {html.escape(str(e))[:200]}")
    msg = "\n\n".join(parts)
    if os.getenv("DRY_RUN") == "1":
        print(msg)
    else:
        send_telegram(msg, os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])


if __name__ == "__main__":
    main()
