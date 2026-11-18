"""
오늘의 부동산 — 강남·서초·송파·용산 아파트 실거래가·신고가
- 데이터: 국토교통부_아파트 매매 실거래가 자료 (DATA_GO_KR_KEY)
- 결과: docs/ 폴더에 구별 웹페이지 생성 (GitHub Pages) + 텔레그램 요약
"""
import html
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from cheongyak_alert import send_telegram

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).parent
DOCS, DATA = HERE / "docs", HERE / "data"
URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
GU = [  # (법정동코드, 이름, 페이지 주소)
    ("11680", "강남구", "gangnam"), ("11650", "서초구", "seocho"),
    ("11710", "송파구", "songpa"), ("11170", "용산구", "yongsan"),
]
HISTORY_MONTHS = 13   # 신고가 비교용 (이번 달 포함 13개월)
RECENT_MONTHS = 3     # 페이지에 보여 줄 기간


def months_back(today, n):
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def fetch(key, lawd, ymd):
    rows, page = [], 1
    while True:
        r = requests.get(URL, params={"serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ymd,
                                      "pageNo": page, "numOfRows": 1000}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        code = root.findtext(".//resultCode")
        if code not in (None, "00", "000"):
            raise RuntimeError(f"{root.findtext('.//resultMsg')} ({code})")
        items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
        rows += items
        if not items or len(rows) >= int(root.findtext(".//totalCount") or 0):
            return rows
        page += 1


PERMIT_URL = "https://land.seoul.go.kr/land/wsklis/getContractList.do"
PERMIT_DAYS = 60      # 토허 내역 조회 기간


def fetch_permits(sgg, begin, end):
    """서울부동산정보광장 '토지거래허가 내역 조회' 화면이 쓰는 데이터 주소"""
    ses = requests.Session()
    ses.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
    ses.get("https://land.seoul.go.kr/land/other/contractStatus.do", timeout=30)
    r = ses.post(PERMIT_URL, data={"sggCd": sgg, "beginDate": begin, "endDate": end}, timeout=30, headers={
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://land.seoul.go.kr/land/other/contractStatus.do"})
    r.raise_for_status()
    out = []
    for x in r.json().get("result") or []:
        addr = (x.get("ADDRESS") or "").strip()
        d = x.get("HNDL_YMD") or ""
        out.append({
            "id": "-".join(str(x.get(k, "")) for k in ("ACC_YEAR", "ACC_NO", "OBJ_SEQNO", "JOB_GBN")),
            "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
            "addr": addr, "key": addr.split(" ", 1)[-1],   # "대치동 316"
            "result": x.get("JOB_GBN_NM", ""), "purpose": x.get("USE_PURP", ""), "jimok": x.get("JIMOK", ""),
        })
    return out


def norm(t):
    try:
        area = float(t.get("excluUseAr") or 0)
        d = f"{int(t['dealYear'])}-{int(t['dealMonth']):02d}-{int(t['dealDay']):02d}"
    except (KeyError, ValueError):
        return None
    row = {
        "apt": t.get("aptNm", ""), "seq": t.get("aptSeq") or t.get("aptNm", ""),
        "dong": t.get("umdNm", ""), "area": round(area, 2), "py": int(area),
        "floor": t.get("floor", ""), "price": int((t.get("dealAmount") or "0").replace(",", "") or 0),
        "date": d, "year": t.get("buildYear", ""), "cancel": t.get("cdealType", "") == "O",
        "kind": t.get("dealingGbn", ""),
        "key": f'{t.get("umdNm", "")} {t.get("jibun", "")}'.strip(),
    }
    row["id"] = "|".join(str(row[k]) for k in ("seq", "date", "floor", "area", "price"))
    return row


def eok(manwon):
    e, r = divmod(manwon, 10000)
    return f"{e}억 {r:,}" if e and r else (f"{e}억" if e else f"{r:,}만")


def build_gu(key, lawd, today):
    rows = []
    for ym in months_back(today, HISTORY_MONTHS):
        try:
            rows += [x for x in map(norm, fetch(key, lawd, ym)) if x]
        except Exception as e:
            print(f"[경고] {lawd} {ym}: {e}", file=sys.stderr)
    # 같은 거래가 '정상'과 '해제'로 둘 다 있으면 해제만 남김
    canceled = {r["id"] for r in rows if r["cancel"]}
    rows = [r for r in rows if r["cancel"] or r["id"] not in canceled]
    # 신고가: 같은 단지·같은 평형(전용 정수㎡)에서 그 이전 13개월 최고가보다 비싸면
    best = {}
    for r in sorted(rows, key=lambda x: x["date"]):
        if r["cancel"]:
            continue
        k = (r["seq"], r["py"])
        prev = best.get(k)
        r["high"] = prev is not None and r["price"] > prev
        r["prev"] = prev
        best[k] = max(prev or 0, r["price"])
    return rows


def main():
    key = os.environ["DATA_GO_KR_KEY"]
    now = datetime.now(KST)
    today = now.date().isoformat()
    cutoff = months_back(now.date(), RECENT_MONTHS)[-1]
    cutoff = f"{cutoff[:4]}-{cutoff[4:]}-01"

    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    seen_path = DATA / "first_seen.json"
    first_run = not seen_path.exists()
    seen = {} if first_run else json.loads(seen_path.read_text())

    pseen_path = DATA / "permit_seen.json"
    pfirst = not pseen_path.exists()
    pseen = {} if pfirst else json.loads(pseen_path.read_text())
    pbegin = (now.date() - timedelta(days=PERMIT_DAYS)).strftime("%Y%m%d")
    pend = now.date().strftime("%Y%m%d")

    summary, new_seen, new_pseen = [], {}, {}
    for lawd, name, slug in GU:
        rows = build_gu(key, lawd, now.date())
        apt_by_key = {}
        for r in rows:
            if r["key"]:
                apt_by_key.setdefault(r["key"], r["apt"])
        try:
            permits = fetch_permits(lawd, pbegin, pend)
            for p in permits:
                p["apt"] = apt_by_key.get(p["key"], "")
                p["seen"] = pseen.get(p["id"]) or ("base" if pfirst else today)
                new_pseen[p["id"]] = p["seen"]
                p["new"] = p["seen"] == today
            permits.sort(key=lambda p: (p["date"], p["id"]), reverse=True)
            perr = ""
        except Exception as e:
            print(f"[경고] {name} 토허 내역 조회 실패: {e}", file=sys.stderr)
            permits, perr = None, str(e)[:120]
        recent = [r for r in rows if r["date"] >= cutoff]
        for r in recent:
            sid = r["id"] + ("|X" if r["cancel"] else "")
            r["seen"] = seen.get(sid) or ("base" if first_run else today)
            new_seen[sid] = r["seen"]
            r["new"] = r["seen"] == today
        recent.sort(key=lambda r: (r["date"], r["price"]), reverse=True)
        new = [r for r in recent if r["new"]]
        highs = [r for r in new if r.get("high")]
        pnew = [p for p in (permits or []) if p["new"]]
        top_apts = {}
        for p in pnew:
            if p["result"] == "허가":
                label = p["apt"] or p["key"]
                top_apts[label] = top_apts.get(label, 0) + 1
        summary.append({"name": name, "slug": slug, "new": len(new), "high": len(highs),
                        "permit_ok": permits is not None, "pnew": len(pnew),
                        "pall": len(permits or []),
                        "ptop": sorted(top_apts.items(), key=lambda kv: -kv[1])[:3],
                        "cancel": sum(r["cancel"] for r in new), "recent": len(recent),
                        "top": sorted(highs, key=lambda r: -r["price"])[:5]})
        page = {"gu": name, "slug": slug, "updated": now.strftime("%Y-%m-%d %H:%M"),
                "first_run": first_run, "rows": recent,
                "permits": permits, "perr": perr, "pfirst": pfirst, "pdays": PERMIT_DAYS}
        (DOCS / f"{slug}.html").write_text(render_gu(page), encoding="utf-8")

    (DOCS / "index.html").write_text(render_index(summary, now, first_run), encoding="utf-8")
    seen_path.write_text(json.dumps(new_seen, ensure_ascii=False))
    if new_pseen:
        pseen_path.write_text(json.dumps({**pseen, **new_pseen} if any(not s["permit_ok"] for s in summary) else new_pseen, ensure_ascii=False))

    site = os.getenv("SITE_URL") or ""
    if not site and os.getenv("GITHUB_REPOSITORY"):
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        site = f"https://{owner}.github.io/{repo}/"
    msg = telegram_text(summary, now, first_run, site)
    if os.getenv("DRY_RUN") == "1":
        print(msg)
    else:
        send_telegram(msg, os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])


def telegram_text(summary, now, first_run, site):
    lines = [f"🏢 <b>오늘의 실거래 · {now.month}/{now.day}</b>"]
    if first_run:
        lines.append("실거래가 페이지를 만들었어요. 내일부터 새로 신고된 거래와 신고가를 알려 드려요.")
    for s in summary:
        link = f'<a href="{site}{s["slug"]}.html">{s["name"]}</a>' if site else s["name"]
        extra = f" · ❌해제 {s['cancel']}" if s["cancel"] else ""
        lines.append(f"\n📍 <b>{link}</b>  신규 {s['new']}건 · 🔺신고가 {s['high']}건{extra}")
        if not s["permit_ok"]:
            lines.append("  🏷 토허 내역: 서울시 서버 연결 실패")
        elif s["pnew"] or not first_run:
            top = ", ".join(f"{html.escape(k)} {v}" for k, v in s["ptop"])
            lines.append(f"  🏷 토허 신규 {s['pnew']}건" + (f" ({top})" if top else ""))
        for r in s["top"]:
            lines.append(f"  🔺 {html.escape(r['apt'])} {r['py']}㎡ {r['floor']}층 <b>{eok(r['price'])}</b>"
                         f" (직전 {eok(r['prev'])})")
    if site:
        lines.append(f'\n🔗 <a href="{site}">전체 보기</a>')
    return "\n".join(lines)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--sub:#6b7280;--line:#e7e9ee;--brand:#2f5bea;--red:#e5484d;--green:#1f9d63}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
a{color:inherit;text-decoration:none}.wrap{max-width:860px;margin:0 auto;padding:16px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin:8px 0 16px}
h1{font-size:22px;margin:0;letter-spacing:-.02em}.upd{color:var(--sub);font-size:12px}
.gus{display:flex;gap:6px;overflow-x:auto;margin-bottom:12px}.gus a{padding:7px 14px;border-radius:999px;background:var(--card);border:1px solid var(--line);font-weight:600;white-space:nowrap}
.gus a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.card h2{margin:0 0 8px;font-size:17px}.big{font-size:26px;font-weight:800;letter-spacing:-.02em}.muted{color:var(--sub);font-size:13px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:4px 0 10px;overflow-x:auto}
.tabs button{border:0;background:none;padding:10px 12px;font:inherit;font-weight:600;color:var(--sub);border-bottom:2px solid transparent;white-space:nowrap;cursor:pointer}
.tabs button.on{color:var(--ink);border-color:var(--ink)}
.tools{display:flex;gap:8px;margin-bottom:10px}.tools input,.tools select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:10px;background:#fff}.tools input{flex:1;min-width:0}
.row{display:flex;justify-content:space-between;gap:10px;padding:12px 4px;border-bottom:1px solid var(--line)}
.row .n{font-weight:700}.row .m{color:var(--sub);font-size:13px}.row .p{text-align:right;white-space:nowrap}.row .p b{font-size:16px}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:1px 6px;border-radius:6px;margin-left:4px;vertical-align:1px}
.hi{background:#ffeceb;color:var(--red)}.cx{background:#eef0f3;color:var(--sub);text-decoration:line-through}.nw{background:#e8efff;color:var(--brand)}
.up{color:var(--red);font-size:12px}.empty{padding:40px 0;text-align:center;color:var(--sub)}
.note{margin-top:18px;color:var(--sub);font-size:12px}
"""

HEAD = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css">'
        '<title>{title}</title><style>' + CSS + '</style></head><body><div class="wrap">')


def nav(active=""):
    links = [f'<a href="index.html" class="{"on" if not active else ""}">전체</a>']
    links += [f'<a href="{s}.html" class="{"on" if s == active else ""}">{n}</a>' for _, n, s in GU]
    return '<nav class="gus">' + "".join(links) + "</nav>"


def render_index(summary, now, first_run):
    cards = "".join(
        f'<a class="card" href="{s["slug"]}.html"><h2>{s["name"]}</h2>'
        f'<div class="big">{s["new"]}<span class="muted"> 건 신규</span></div>'
        f'<div class="muted">🔺신고가 {s["high"]}건 · 최근 3개월 {s["recent"]:,}건</div>'
        f'<div class="muted">🏷 토허 신규 {s["pnew"] if s["permit_ok"] else "-"}건 · 최근 {PERMIT_DAYS}일 {s["pall"]:,}건</div></a>'
        for s in summary)
    first = '<p class="muted">첫 수집일이라 신규 건수는 내일부터 집계돼요.</p>' if first_run else ""
    return (HEAD.replace("{title}", "오늘의 부동산") +
            f'<header><h1>오늘의 부동산</h1><span class="upd">{now:%Y-%m-%d %H:%M} 업데이트</span></header>'
            + nav() + first + f'<div class="grid">{cards}</div>'
            '<p class="note">출처: 국토교통부 아파트 매매 실거래가 (매일 아침 자동 갱신). '
            '실거래는 계약 후 30일 안에 신고되므로 신규 신고 건의 계약일은 며칠~몇 주 전일 수 있어요. '
            '신고가 = 같은 단지·같은 전용면적의 직전 1년 최고가 경신.</p></div></body></html>')


def render_gu(page):
    data = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    return (HEAD.replace("{title}", f"{page['gu']} 실거래 · 오늘의 부동산") +
            f'<header><h1>{page["gu"]}</h1><span class="upd">{page["updated"]} 업데이트</span></header>'
            + nav(page["slug"]) +
            '<div class="tabs"><button data-t="new" class="on">오늘의 실거래</button>'
            '<button data-t="high">오늘의 신고가</button><button data-t="all">최근 3개월</button>'
            '<button data-t="permit">토지거래허가</button></div>'
            '<div class="tools"><input id="q" placeholder="단지·동 이름 검색">'
            '<select id="s"><option value="date">계약일순</option><option value="price">가격순</option></select></div>'
            '<div id="list"></div><p class="note">출처: 국토교통부 아파트 매매 실거래가, 서울부동산정보광장 토지거래허가 내역. 🔺신고가 = 같은 단지·같은 전용면적 직전 1년 최고가 경신. 토허 단지명은 지번으로 추정한 것이라 일부는 주소로만 표시돼요.</p>'
            f'</div><script>const D={data};' + JS + '</script></body></html>')


JS = r"""
const $=s=>document.querySelector(s);let tab='new';
const eok=m=>{const e=Math.floor(m/1e4),r=m%1e4;return e&&r?`${e}억 ${r.toLocaleString()}`:e?`${e}억`:`${r.toLocaleString()}만`};
function rows(){let r=D.rows;const q=$('#q').value.trim();
 if(tab==='new')r=r.filter(x=>x.new);if(tab==='high')r=r.filter(x=>x.high&&(x.new||D.first_run));
 if(q)r=r.filter(x=>(x.apt+x.dong).includes(q));
 r=[...r].sort($('#s').value==='price'?(a,b)=>b.price-a.price:(a,b)=>b.date.localeCompare(a.date)||b.price-a.price);return r}
function draw(){const L=$('#list');$('#s').style.display=tab==='permit'?'none':'';
 if(tab==='permit'){drawPermit(L);return}
 const r=rows();if(!r.length){L.innerHTML=`<div class="empty">${tab==='new'&&D.first_run?'첫 수집일이라 내일부터 신규 거래가 표시돼요.':'해당 거래가 없어요.'}</div>`;return}
 L.innerHTML=r.slice(0,500).map(x=>`<div class="row"><div><div class="n">${x.apt}${x.high?'<span class="tag hi">신고가</span>':''}${x.cancel?'<span class="tag cx">해제</span>':''}${x.new&&tab==='all'?'<span class="tag nw">NEW</span>':''}</div>
 <div class="m">${x.dong} · 전용 ${x.area}㎡ · ${x.floor}층 · ${x.year}년식</div></div>
 <div class="p"><b>${eok(x.price)}</b><div class="m">${x.date.slice(2).replace(/-/g,'.')} 계약</div>${x.high&&x.prev?`<div class="up">▲ 직전 ${eok(x.prev)}</div>`:''}</div></div>`).join('')+(r.length>500?`<div class="empty">상위 500건만 표시</div>`:'')}
function drawPermit(L){const P=D.permits;
 if(!P){L.innerHTML=`<div class="empty">서울시 토지거래허가 서버에 연결하지 못했어요.<br><span class="m">${D.perr||''}</span></div>`;return}
 const q=$('#q').value.trim();let r=q?P.filter(x=>(x.apt+x.addr).includes(q)):P;
 const nNew=P.filter(x=>x.new).length,cnt={};
 r.filter(x=>x.result==='허가').forEach(x=>{const k=x.apt||x.key;cnt[k]=cnt[k]||{n:0,t:0,addr:x.addr};cnt[k].n++;if(x.new)cnt[k].t++});
 const top=Object.entries(cnt).sort((a,b)=>b[1].n-a[1].n).slice(0,10);
 const tag=x=>x.result==='허가'?'':`<span class="tag ${x.result==='불허가'?'hi':'cx'}">${x.result}</span>`;
 L.innerHTML=`<div class="card" style="margin-bottom:12px"><div class="muted">${D.pfirst?'첫 수집일이라 신규는 내일부터 집계돼요 · ':`오늘 신규 <b>${nNew}</b>건 · `}최근 ${D.pdays}일 ${P.length.toLocaleString()}건</div>
 <div style="margin-top:8px;font-weight:700">단지별 허가 TOP 10</div>${top.map(([k,v],i)=>`<div class="row" style="padding:8px 2px"><div>${i+1}. ${k}<div class="m">${v.addr}</div></div><div class="p"><b>${v.n}건</b>${v.t?`<div class="up">오늘 +${v.t}</div>`:''}</div></div>`).join('')||'<div class="m">없음</div>'}</div>`+
 r.slice(0,400).map(x=>`<div class="row"><div><div class="n">${x.apt||x.key}${tag(x)}${x.new?'<span class="tag nw">NEW</span>':''}</div><div class="m">${x.addr} · ${x.purpose} · 지목 ${x.jimok}</div></div><div class="p"><div class="m">${x.date.slice(2).replace(/-/g,'.')} 처리</div></div></div>`).join('')}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');tab=b.dataset.t;draw()});
$('#q').oninput=draw;$('#s').onchange=draw;draw();
"""

if __name__ == "__main__":
    main()
