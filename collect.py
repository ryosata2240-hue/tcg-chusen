#!/usr/bin/env python3
"""トレカ抽選情報を集めて docs/index.html（一覧ページ）を作る。

情報源: 入荷Now（https://nyuka-now.com）の抽選まとめページ。
15〜30分ごとに GitHub Actions から実行される想定。標準ライブラリのみ使用。
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
STATE = ROOT / "state" / "seen.json"
JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)
UA = "Mozilla/5.0 (personal lottery-info reader; low frequency)"

# 情報源: (タイトル, URL, 種類) 種類 list=まとめページ / product=商品別ページ
SOURCES = [
    ("ポケモンカード", "https://nyuka-now.com/archives/2459", "list"),
    ("ポケモンカード", "https://nyuka-now.com/archives/157639", "list"),  # 30周年記念商品
    ("遊戯王", "https://nyuka-now.com/archives/72605", "list"),
    ("遊戯王", "https://nyuka-now.com/archives/100529", "list"),  # ラッシュデュエル
    ("ドラゴンボール", "https://nyuka-now.com/archives/141863", "list"),
]
# ワンピースは全体まとめページが無いため、新しい商品別ページを検索で見つける
ONEPIECE_SEARCH = "https://nyuka-now.com/?s=ONE+PIECE%E3%82%AB%E3%83%BC%E3%83%89%E3%82%B2%E3%83%BC%E3%83%A0+%E6%8A%BD%E9%81%B8%E4%BA%88%E7%B4%84&feed=rss2"
ONEPIECE_PAGES = 8

# 「拡張パックと限定商品」に絞るため除外する商品キーワード
EXCLUDE_PRODUCT = re.compile(r"スリーブ|デッキシールド|デッキケース|プレイマット|ローダー|グミ|食玩|ウエハース|Tシャツ|ぬいぐるみ")
# ポケモン30周年ページはカード以外（服・時計など）も載るので、カード関連だけ残す
CARD_WORD = re.compile(r"カード|BOX|ボックス|パック|デッキ|TCG")

LIST_SECTIONS = ("抽選・予約応募受付中", "近日受付開始", "会員限定")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


class Tokenizer(HTMLParser):
    """見出しと表の行だけを順番に取り出す。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.cur = None  # 収集中の見出し or セル
        self.buf = []
        self.links = []
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3", "h4"):
            self.cur, self.buf = tag, []
        elif tag == "tr":
            self.row = {"th": "", "td": "", "links": []}
        elif tag in ("th", "td") and self.row is not None:
            self.cur, self.buf = tag, []
        elif tag == "a" and self.cur == "td":
            href = dict(attrs).get("href")
            if href:
                self.row["links"].append(href)
        elif tag in ("br", "li", "p") and self.cur == "td":
            self.buf.append("\n")
        elif tag == "table":
            self.events.append(("table", None))

    def handle_endtag(self, tag):
        if tag == self.cur and tag in ("h2", "h3", "h4"):
            self.events.append((tag, clean("".join(self.buf))))
            self.cur = None
        elif tag in ("th", "td") and tag == self.cur and self.row is not None:
            self.row[tag] = clean("".join(self.buf), keep_newlines=(tag == "td"))
            self.cur = None
        elif tag == "tr" and self.row is not None:
            if self.row["th"]:
                self.events.append(("row", self.row))
            self.row = None

    def handle_data(self, data):
        if self.cur:
            self.buf.append(data)


def clean(s, keep_newlines=False):
    s = unescape(s)
    if keep_newlines:
        lines = [re.sub(r"\s+", " ", x).strip() for x in s.split("\n")]
        return "\n".join(x for x in lines if x)
    return re.sub(r"\s+", " ", s).strip()


def parse_page(html, game, url, kind):
    tok = Tokenizer()
    tok.feed(html)
    entries, section, shop, status, updated, cur = [], "", "", "", "", None

    def close():
        if cur and "対象商品" in cur["fields"]:
            entries.append(cur)

    for ev, val in tok.events:
        if ev == "h2":
            close(); cur = None
            section = val
        elif ev == "h3":
            close(); cur = None
            updated = ""
            m = re.match(r"(.*?)\s*(終了|受付中|近日|在庫あり|再販可能性あり|履歴なし)?$", val)
            shop, status = m.group(1).strip(), (m.group(2) or "")
        elif ev in ("h4", "table"):
            close()
            if ev == "h4":
                updated = val
            cur = {"game": game, "source": url, "kind": kind, "section": section,
                   "shop": shop, "status": status, "updated": updated, "fields": {}, "links": {}}
        elif ev == "row" and cur is not None:
            k = val["th"]
            if k in cur["fields"]:  # 表の区切りが無いまま次の商品が始まった
                close()
                cur = dict(cur, fields={}, links={})
            cur["fields"][k] = val["td"]
            if val["links"]:
                cur["links"][k] = val["links"][0]
    close()

    out = []
    for e in entries:
        if kind == "list" and not any(s in e["section"] for s in LIST_SECTIONS):
            continue
        if kind == "product" and "抽選・予約状況" not in e["section"]:
            continue
        item = normalize(e)
        if item:
            out.append(item)
    return out


WEEKDAYS = "月火水木金土日"


def parse_date(text, ref=None):
    """「9月29日(火)11:59」などを datetime にする。
    年が書かれていない場合は、曜日が合う年のうち基準日（記事の更新日）に最も近い年を採用。"""
    if not text:
        return None
    m = re.search(r"(?:(\d{4})年)?\s*(\d{1,2})月(\d{1,2})日\s*(?:[(（]([月火水木金土日])[)）])?[^\d]*(?:(\d{1,2}):(\d{2}))?", text)
    if not m:
        return None
    y, mo, d, wd, hh, mm = m.groups()
    hh, mm = (int(hh), int(mm)) if hh else (23, 59)
    if hh >= 24:
        hh, mm = 23, 59
    ref = ref or NOW
    years = [int(y)] if y else range(ref.year - 3, ref.year + 2)
    cands = []
    for yy in years:
        try:
            dt = datetime(yy, int(mo), int(d), hh, mm, tzinfo=JST)
        except ValueError:
            continue
        if wd and not y and WEEKDAYS[dt.weekday()] != wd:
            continue
        cands.append(dt)
    return min(cands, key=lambda dt: abs(dt - ref)) if cands else None


def normalize(e):
    f = e["fields"]
    fmt = f.get("抽選形式") or f.get("販売形式") or ""
    if not re.search(r"抽選|招待", fmt):
        return None  # 先着・予約・イベントは対象外
    products = [p for p in f.get("対象商品", "").split("\n") if p]
    detail = f.get("対象商品詳細", "")
    if e["game"] == "ポケモンカード" and not CARD_WORD.search(" ".join(products) + detail):
        return None
    products = [p for p in products if not EXCLUDE_PRODUCT.search(p)] or []
    if not products:
        return None

    ref = parse_date(e.get("updated", "")) or NOW
    start, end = parse_date(f.get("開始日"), ref), parse_date(f.get("終了日"), ref)
    if end and end < NOW:
        return None
    if not end:
        # 締切不明は、終了扱いの見出しか、開始から3週間以上たったものを除外
        if e["status"] == "終了":
            return None
        if start and start < NOW - timedelta(days=21):
            return None
    if e["kind"] == "product" and e["status"] == "終了" and not end:
        return None

    url = ""
    for k in ("応募ページ", "詳細ページ", "販売ページ"):
        if k in e["links"]:
            url = e["links"][k]
            break
    item = {
        "game": detect_game(" ".join(products)) or e["game"],
        "shop": e["shop"],
        "products": products,
        "format": fmt,
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "start_text": f.get("開始日", ""),
        "end_text": f.get("終了日", ""),
        "announce": f.get("当選発表", ""),
        "conditions": f.get("応募条件", "") or detail,
        "url": url,
        "source": e["source"],
        "upcoming": bool(start and start > NOW) or "近日" in e["section"],
        "members_only": "会員限定" in e["section"],
    }
    item["area"], item["area_note"] = classify(item)
    return item


GAME_WORDS = [
    ("ポケモンカード", r"ポケモンカード|ポケカ"),
    ("遊戯王", r"遊戯王"),
    ("ワンピース", r"ONE ?PIECE|ワンピース"),
    ("ドラゴンボール", r"ドラゴンボール|フュージョンワールド"),
]


def detect_game(text):
    hits = [g for g, pat in GAME_WORDS if re.search(pat, text)]
    return hits[0] if len(hits) == 1 else ("複数タイトル" if hits else None)


STORES = json.loads((ROOT / "stores.json").read_text(encoding="utf-8"))


def classify(it):
    """net=ネット完結 / local=札幌近郊で可 / check=要確認 / none=札幌近郊では不可"""
    text = " ".join([it["shop"], it["format"], it["conditions"]])
    if re.search(r"北海道(を)?除|北海道.{0,8}(対象外|除外)", text):
        return "none", "北海道は対象外"
    if any(w in text for w in STORES["local_words"]):
        return "local", "北海道・札幌の記載あり"
    online = re.search(r"オンライン販売|配送|発送|通販", it["format"])
    in_store = "店頭" in it["format"]
    if online and not in_store:
        return "net", "ネットで応募・配送"
    shop = it["shop"]
    if any(k in shop for k in STORES["no"]):
        return "none", "札幌近郊に店舗なし（判定表）"
    if any(w in shop for w in STORES["no_place_words"]):
        return "none", "北海道以外の店舗"
    if any(k in shop for k in STORES["yes"]):
        return "local", "札幌近郊に店舗あり（対象店舗は要確認）"
    if not in_store and re.search(r"オンライン|通販|Amazon|楽天|バンダイ|STYLE|ストア|\.com", shop, re.I):
        return "net", "ネット応募"
    return "check", "札幌近郊の店舗有無が不明"


def key_of(it):
    return "|".join([it["shop"], "/".join(it["products"]), it["start_text"], it["end_text"]])


def onepiece_sources():
    try:
        rss = fetch(ONEPIECE_SEARCH)
    except Exception as ex:
        print("ワンピース検索に失敗:", ex, file=sys.stderr)
        return []
    links = [u for t, u in re.findall(r"<item>\s*<title>([^<]*)</title>\s*<link>([^<]+)", rss)
             if "ONE PIECE" in t]
    links = sorted(set(links), key=lambda u: int(re.sub(r"\D", "", u) or 0), reverse=True)
    return [("ワンピース", u, "product") for u in links[:ONEPIECE_PAGES]]


def main():
    items, errors = [], []
    for game, url, kind in SOURCES + onepiece_sources():
        try:
            items += parse_page(fetch(url), game, url, kind)
        except Exception as ex:
            errors.append(f"{url}: {ex}")
        time.sleep(1.5)  # 相手サイトへの負荷を抑える

    # 重複除去
    uniq = {}
    for it in items:
        uniq.setdefault(key_of(it), it)
    items = list(uniq.values())

    # 初めて見つけた時刻を記録（NEW表示用）
    seen = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    first_run = not seen
    for it in items:
        k = key_of(it)
        if k not in seen:
            seen[k] = (NOW - timedelta(days=2)).isoformat() if first_run else NOW.isoformat()
        it["first_seen"] = seen[k]
    cutoff = (NOW - timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")

    items.sort(key=lambda x: (x["upcoming"], x["end"] or "9999"))
    data = {"updated": NOW.isoformat(), "errors": errors, "items": items}
    DOCS.mkdir(exist_ok=True)
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (DOCS / "index.html").write_text(tpl.replace("/*DATA*/null", payload), encoding="utf-8")
    print(f"{len(items)}件 / エラー{len(errors)}件")
    for e in errors:
        print("  ", e, file=sys.stderr)


if __name__ == "__main__":
    main()
