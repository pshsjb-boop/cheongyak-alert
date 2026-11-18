"""
청약홈 청약 일정 매일 텔레그램 알림
- 데이터: 공공데이터포털 '한국부동산원_청약홈 분양정보 조회 서비스' (청약홈 캘린더와 같은 원천)
- 알림: 텔레그램 봇

환경변수
  DATA_GO_KR_KEY      공공데이터포털 인증키 (Decoding 키)
  TELEGRAM_BOT_TOKEN  텔레그램 봇 토큰
  TELEGRAM_CHAT_ID    받을 사람 chat id
  REGIONS             (선택) 관심 지역. 예: "서울,경기,인천"  비우면 전국
  INCLUDE_OFFICETEL   (선택) "1"이면 오피스텔/도시형/민간임대도 포함 (기본 0)
  SKIP_EMPTY          (선택) "1"이면 알릴 게 없는 날은 메시지 안 보냄 (기본 0)
  DRY_RUN             (선택) "1"이면 전송 대신 화면에 출력
"""
import html
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"
SEEN_FILE = Path(__file__).with_name("seen.json")
LOOKBACK_DAYS = 90      # 공고일 기준 최근 90일치 공고를 받아 일정 계산
UPCOMING_DAYS = 7       # '다가오는 일정' 범위
WEEKDAY = "월화수목금토일"

# (API 오퍼레이션, 분류명, [(일정 이름, 시작일 필드, 종료일 필드)])
SOURCES = [
    ("getAPTLttotPblancDetail", "APT", [
        ("특별공급", "SPSPLY_RCEPT_BGNDE", "SPSPLY_RCEPT_ENDDE"),
        ("1순위(해당지역)", "GNRL_RNK1_CRSPAREA_RCPTDE", "GNRL_RNK1_CRSPAREA_ENDDE"),
        ("1순위(기타경기)", "GNRL_RNK1_ETC_GG_RCPTDE", "GNRL_RNK1_ETC_GG_ENDDE"),
        ("1순위(기타지역)", "GNRL_RNK1_ETC_AREA_RCPTDE", "GNRL_RNK1_ETC_AREA_ENDDE"),
        ("2순위(해당지역)", "GNRL_RNK2_CRSPAREA_RCPTDE", "GNRL_RNK2_CRSPAREA_ENDDE"),
        ("2순위(기타경기)", "GNRL_RNK2_ETC_GG_RCPTDE", "GNRL_RNK2_ETC_GG_ENDDE"),
        ("2순위(기타지역)", "GNRL_RNK2_ETC_AREA_RCPTDE", "GNRL_RNK2_ETC_AREA_ENDDE"),
        ("당첨자발표", "PRZWNER_PRESNATN_DE", None),
        ("계약", "CNTRCT_CNCLS_BGNDE", "CNTRCT_CNCLS_ENDDE"),
    ]),
    ("getRemndrLttotPblancDetail", "무순위/잔여", [
        ("특별공급", "SPSPLY_RCEPT_BGNDE", "SPSPLY_RCEPT_ENDDE"),
        ("접수", "SUBSCRPT_RCEPT_BGNDE", "SUBSCRPT_RCEPT_ENDDE"),
        ("일반공급", "GNRL_RCEPT_BGNDE", "GNRL_RCEPT_ENDDE"),
        ("당첨자발표", "PRZWNER_PRESNATN_DE", None),
    ]),
    ("getOPTLttotPblancDetail", "임의공급", [
        ("특별공급", "SPSPLY_RCEPT_BGNDE", "SPSPLY_RCEPT_ENDDE"),
        ("접수", "SUBSCRPT_RCEPT_BGNDE", "SUBSCRPT_RCEPT_ENDDE"),
        ("일반공급", "GNRL_RCEPT_BGNDE", "GNRL_RCEPT_ENDDE"),
        ("당첨자발표", "PRZWNER_PRESNATN_DE", None),
    ]),
]
OFFICETEL_SOURCES = [
    ("getUrbtyOfctlLttotPblancDetail", "오피스텔/도시형", [
        ("접수", "SUBSCRPT_RCEPT_BGNDE", "SUBSCRPT_RCEPT_ENDDE"),
        ("당첨자발표", "PRZWNER_PRESNATN_DE", None),
    ]),
    ("getPblPvtRentLttotPblancDetail", "공공지원민간임대", [
        ("접수", "SUBSCRPT_RCEPT_BGNDE", "SUBSCRPT_RCEPT_ENDDE"),
        ("당첨자발표", "PRZWNER_PRESNATN_DE", None),
    ]),
]


def parse_date(v):
    if not v:
        return None
    s = "".join(ch for ch in str(v) if ch.isdigit())[:8]
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def fmt_d(d):
    return f"{d.month}/{d.day}({WEEKDAY[d.weekday()]})"


def fetch(op, key, since):
    rows, page = [], 1
    while True:
        r = requests.get(BASE + op, params={
            "page": page, "perPage": 500, "serviceKey": key,
            "cond[RCRIT_PBLANC_DE::GTE]": since.isoformat(),
        }, timeout=30)
        r.raise_for_status()
        js = r.json()
        data = js.get("data") or []
        rows += data
        total = js.get("matchCount") or js.get("totalCount") or 0
        if not data or len(rows) >= total:
            return rows
        page += 1


def region_ok(row, regions):
    if not regions:
        return True
    text = f"{row.get('SUBSCRPT_AREA_CODE_NM') or ''} {row.get('HSSPLY_ADRES') or ''}"
    return any(r in text for r in regions)


def item_key(row):
    return f"{row.get('HOUSE_MANAGE_NO')}-{row.get('PBLANC_NO')}"


def link(row):
    url = row.get("PBLANC_URL") or row.get("HMPG_ADRES") or ""
    name = html.escape(row.get("HOUSE_NM") or "(이름 없음)")
    return f'<a href="{html.escape(url)}">{name}</a>' if url.startswith("http") else name


def area(row):
    a = row.get("SUBSCRPT_AREA_CODE_NM")
    if not a:
        a = (row.get("HSSPLY_ADRES") or "").split(" ")[0]
    return html.escape(a or "")


def build_message(items, today, seen, first_run):
    today_lines, upcoming, new_lines = [], [], []
    for cat, row, schedule in items:
        tag = f"[{area(row)}·{cat}]" if area(row) else f"[{cat}]"
        for label, bf, ef in schedule:
            b = parse_date(row.get(bf))
            e = parse_date(row.get(ef)) if ef else b
            if not b:
                continue
            e = e or b
            if b <= today <= e:
                until = "" if b == e else f" (~{fmt_d(e)})"
                today_lines.append(f"• {tag} {link(row)} — <b>{label}</b>{until}")
            elif today < b <= today + timedelta(days=UPCOMING_DAYS):
                upcoming.append((b, f"• {fmt_d(b)} {tag} {link(row)} — {label}"))

        k = item_key(row)
        pub = parse_date(row.get("RCRIT_PBLANC_DE"))
        is_new = (k not in seen) and not (first_run and pub and pub < today - timedelta(days=3))
        if is_new:
            supply = row.get("TOT_SUPLY_HSHLDCO")
            supply = f", {supply}세대" if supply else ""
            pub_s = f"공고 {fmt_d(pub)}" if pub else ""
            new_lines.append(f"• {tag} {link(row)}\n   {html.escape(row.get('HSSPLY_ADRES') or '')}{supply} · {pub_s}")

    upcoming.sort(key=lambda x: x[0])
    parts = [f"🏠 <b>청약 알림 · {fmt_d(today)}</b>"]
    if new_lines:
        parts.append("🆕 <b>새로 올라온 공고</b>\n" + "\n".join(new_lines))
    parts.append("📌 <b>오늘 일정</b>\n" + ("\n".join(today_lines) if today_lines else "• 없음"))
    if upcoming:
        parts.append(f"🗓 <b>다가오는 일정 ({UPCOMING_DAYS}일)</b>\n" + "\n".join(x[1] for x in upcoming))
    parts.append('🔗 <a href="https://www.applyhome.co.kr/ai/aib/selectSubscrptCalenderView.do">청약홈 캘린더 열기</a>')
    has_content = bool(new_lines or today_lines or upcoming)
    return "\n\n".join(parts), has_content


def send_telegram(text, token, chat_id):
    # 텔레그램 한 메시지 최대 4096자 → 문단 단위로 나눠 전송
    chunks, cur = [], ""
    for block in text.split("\n"):
        if len(cur) + len(block) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += block + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={
            "chat_id": chat_id, "text": c, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }, timeout=30)
        r.raise_for_status()


def site_url():
    if os.getenv("SITE_URL"):
        return os.environ["SITE_URL"]
    if os.getenv("GITHUB_REPOSITORY"):
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        return f"https://{owner}.github.io/{repo}/"
    return ""


def save_seoul(seoul, today, seen, first_run):
    """웹페이지용 서울 청약 데이터 (data/cheongyak_seoul.json)"""
    out = []
    for cat, row, schedule in seoul:
        sch = []
        for label, bf, ef in schedule:
            b = parse_date(row.get(bf))
            if not b:
                continue
            e = (parse_date(row.get(ef)) if ef else b) or b
            sch.append({"label": label, "b": b.isoformat(), "e": e.isoformat()})
        pub = parse_date(row.get("RCRIT_PBLANC_DE"))
        out.append({
            "name": row.get("HOUSE_NM") or "", "cat": cat, "area": area(row) and html.unescape(area(row)),
            "addr": row.get("HSSPLY_ADRES") or "", "supply": row.get("TOT_SUPLY_HSHLDCO") or "",
            "url": row.get("PBLANC_URL") or row.get("HMPG_ADRES") or "",
            "pub": pub.isoformat() if pub else "", "sch": sorted(sch, key=lambda x: x["b"]),
            "new": (not first_run) and item_key(row) not in seen,
        })
    data_dir = Path(__file__).with_name("data")
    data_dir.mkdir(exist_ok=True)
    (data_dir / "cheongyak_seoul.json").write_text(json.dumps(
        {"today": today.isoformat(), "items": out}, ensure_ascii=False))


def main():
    key = os.environ["DATA_GO_KR_KEY"]
    regions = [r.strip() for r in os.getenv("REGIONS", "").split(",") if r.strip()]
    dry = os.getenv("DRY_RUN") == "1"
    today = datetime.now(KST).date()
    since = today - timedelta(days=LOOKBACK_DAYS)

    sources = SOURCES + (OFFICETEL_SOURCES if os.getenv("INCLUDE_OFFICETEL") == "1" else [])
    items, seoul = [], []
    for op, cat, schedule in sources:
        try:
            for row in fetch(op, key, since):
                if region_ok(row, regions):
                    items.append((cat, row, schedule))
                if region_ok(row, ["서울"]):
                    seoul.append((cat, row, schedule))
        except Exception as e:  # 한 종류가 실패해도 나머지는 보냄
            print(f"[경고] {cat} 조회 실패: {e}", file=sys.stderr)

    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()
    msg, has_content = build_message(items, today, seen, first_run)
    site = site_url()
    if site:
        msg += f'\n🌐 <a href="{site}cheongyak.html">서울 청약 일정 페이지</a>'
    save_seoul(seoul, today, seen, first_run)

    if dry:
        print(msg)
    elif has_content or os.getenv("SKIP_EMPTY") != "1":
        send_telegram(msg, os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])

    seen |= {item_key(r) for _, r, _ in items}
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False))


if __name__ == "__main__":
    main()
