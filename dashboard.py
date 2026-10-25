#!/usr/bin/env python3
"""Kindle Paperwhite 5 (Signature Edition) 用ダッシュボード画像生成スクリプト

天気(気象庁) / 鉄道運行情報 / ニュース(NHK RSS) を取得して、
1236x1648 のグレースケールPNGを書き出す。

使い方:
    python dashboard.py                 # 実データで out/dashboard.png を生成
    python dashboard.py --mock          # サンプルデータでレイアウトだけ確認
    python dashboard.py --dump          # 取得した生データを out/raw/ に保存(デバッグ用)

依存: Pillow のみ (pip install pillow)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
CONFIG = {
    "place_label": "大阪市東住吉区",
    # 気象庁: 大阪府の府県予報区コード。気温は「大阪」地点(62078)
    "jma_office": "270000",
    "jma_temp_area": "62078",
    # 表示する鉄道路線。match は Yahoo!路線情報 / JR西日本の路線名に含まれる文字列
    # JR は match="*" で近畿エリアの全路線を対象にし、遅れのある路線名を列挙する
    "train_lines": [
        {"label": "JR西日本(近畿)", "match": "*", "source": "jrwest"},
        {"label": "御堂筋線", "match": "御堂筋線", "source": "yahoo"},
        {"label": "谷町線", "match": "谷町線", "source": "yahoo"},
        {"label": "京阪電車", "match": "京阪", "source": "yahoo"},  # 京阪本線・中之島線など
    ],
    # Yahoo!路線情報の近畿エリア一覧ページ(遅延のある路線だけが並ぶ)
    "yahoo_area_url": "https://transit.yahoo.co.jp/diainfo/area/6",
    "jrwest_base": "https://www.train-guide.westjr.co.jp/api/v3",
    "news_rss": "https://www.nhk.or.jp/rss/news/cat0.xml",
    "news_count": 6,
    # 1時間降水量(Open-Meteo)。東住吉区役所付近の座標
    "lat_lon": (34.6142, 135.5285),
    "rain_hours": 12,
    # 市況の更新時刻(JST)。それ以外の実行時は前回取得分(キャッシュ)を表示する
    # 7:00 = 米国市場の終値確定後 / 16:00 = 東京市場の終値確定後
    "market_update_times": ["07:00", "16:00"],
    # 市況。sources は上から順に試す (提供元, シンボル)
    "markets": [
        {"label": "ドル円", "kind": "fx", "sources": [("yahoo", "JPY=X"), ("stooq", "usdjpy")]},
        {"label": "日経平均", "sources": [("yahoo", "^N225"), ("stooq", "^nkx")]},
        {"label": "TOPIX", "sources": [("stooq", "^tpx"), ("yahoo", "^TOPX")]},
        {"label": "ダウ", "sources": [("yahoo", "^DJI"), ("stooq", "^dji")]},
        {"label": "NASDAQ", "sources": [("yahoo", "^IXIC"), ("stooq", "^ndq")]},
        {"label": "WTI原油", "kind": "usd", "sources": [("yahoo", "CL=F"), ("stooq", "cl.f")]},
        {"label": "米10年債", "kind": "yield", "sources": [("yahoo", "^TNX"), ("stooq", "10yusy.b")]},
    ],
    "size": (1648, 1236),  # Paperwhite 5 を横向きに置いた状態で描く
    # Kindle の画面は縦長固定なので、保存時に回転させる。
    # 270 = 本体を時計回りに90度倒して置く(電源ボタンが上)想定。逆なら 90 に。
    "rotate": 270,
}

UA = "Mozilla/5.0 (personal e-ink dashboard)"
JST = dt.timezone(dt.timedelta(hours=9))
WEEKDAYS = "月火水木金土日"


# ------------------------------------------------------------------
# 取得
# ------------------------------------------------------------------
def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@dataclass
class DayWeather:
    date: dt.date
    code: str = ""
    text: str = ""
    tmax: str = ""
    tmin: str = ""
    pops: list[str] = field(default_factory=list)  # 6時間ごと(今日・明日のみ)
    pop: str = ""  # 週間予報の日別降水確率


@dataclass
class Weather:
    days: list[DayWeather]  # [0]=今日, [1]=明日, 以降週間


def parse_jma(forecast: list) -> Weather:
    short, week = forecast[0], forecast[1]
    days: dict[dt.date, DayWeather] = {}

    def get_day(iso: str) -> DayWeather:
        d = dt.datetime.fromisoformat(iso).date()
        if d not in days:
            days[d] = DayWeather(date=d)
        return days[d]

    # 短期予報: 天気
    ts0 = short["timeSeries"][0]
    area0 = ts0["areas"][0]
    for i, t in enumerate(ts0["timeDefines"]):
        day = get_day(t)
        day.code = area0["weatherCodes"][i]
        day.text = area0["weathers"][i].replace("\u3000", "")

    # 短期予報: 6時間降水確率
    ts1 = short["timeSeries"][1]
    for i, t in enumerate(ts1["timeDefines"]):
        get_day(t).pops.append(ts1["areas"][0]["pops"][i])

    # 短期予報: 気温(朝の最低/日中の最高。時間帯で並びが変わるので時刻で判定)
    ts2 = short["timeSeries"][2]
    temp_area = next(
        (a for a in ts2["areas"] if a["area"]["code"] == CONFIG["jma_temp_area"]),
        ts2["areas"][0],
    )
    for i, t in enumerate(ts2["timeDefines"]):
        tm = dt.datetime.fromisoformat(t)
        day = get_day(t)
        if tm.hour == 0:
            day.tmin = temp_area["temps"][i]
        else:
            day.tmax = temp_area["temps"][i]

    # 週間予報: 天気・降水確率
    w0 = week["timeSeries"][0]
    for i, t in enumerate(w0["timeDefines"]):
        day = get_day(t)
        a = w0["areas"][0]
        if not day.code:
            day.code = a["weatherCodes"][i]
        day.pop = a["pops"][i] if i < len(a["pops"]) else ""

    # 週間予報: 最高最低
    w1 = week["timeSeries"][1]
    wa = w1["areas"][0]
    for i, t in enumerate(w1["timeDefines"]):
        day = get_day(t)
        if not day.tmax and wa["tempsMax"][i]:
            day.tmax = wa["tempsMax"][i]
        if not day.tmin and wa["tempsMin"][i]:
            day.tmin = wa["tempsMin"][i]

    today = dt.datetime.now(JST).date()
    ordered = [days[d] for d in sorted(days) if d >= today]
    # 短期予報の今日の最低気温は朝の時点を過ぎると消えるので「-」になる
    return Weather(days=ordered)


def fetch_weather(dump_dir: Path | None) -> Weather:
    url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{CONFIG['jma_office']}.json"
    raw = http_get(url)
    if dump_dir:
        (dump_dir / "jma.json").write_bytes(raw)
    return parse_jma(json.loads(raw))


@dataclass
class TrainStatus:
    label: str
    status: str  # "平常運転" / "遅延" など。取得失敗は "取得できず"
    detail: str = ""
    ok: bool | None = None  # True=平常, False=異常, None=不明
    lines: list[str] = field(default_factory=list)  # 遅れのある路線名(JR一括表示用)


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def fetch_jrwest(lines: list[dict], dump_dir: Path | None) -> dict[str, TrainStatus]:
    """JR西日本「列車走行位置」サイトが使う非公式JSON。仕様は予告なく変わりうる。"""
    out: dict[str, TrainStatus] = {}
    base = CONFIG["jrwest_base"]
    master = json.loads(http_get(f"{base}/area_kinki_master.json"))
    info_raw = http_get(f"{base}/area_kinki_trafficinfo.json")
    if dump_dir:
        (dump_dir / "jrwest_master.json").write_text(json.dumps(master, ensure_ascii=False, indent=1), encoding="utf-8")
        (dump_dir / "jrwest_traffic.json").write_bytes(info_raw)
    info = json.loads(info_raw)
    troubled = info.get("lines", {}) or {}

    name_to_key = {}
    for key, v in (master.get("lines") or {}).items():
        name_to_key[v.get("name", "")] = key

    key_to_name = {k: n for n, k in name_to_key.items()}
    for ln in lines:
        if ln["match"] == "*":
            if troubled:
                names = [key_to_name.get(k, k) for k in troubled]
                details = []
                for k, t in troubled.items():
                    details.append(f"{key_to_name.get(k, k)}: " + " ".join(_flatten_strings(t)))
                out[ln["label"]] = TrainStatus(ln["label"], "遅れあり", " / ".join(details)[:160], False)
                out[ln["label"]].lines = names
            else:
                out[ln["label"]] = TrainStatus(ln["label"], "平常運転", "", True)
            continue
        key = next((k for n, k in name_to_key.items() if ln["match"] in n), None)
        if key is None:
            out[ln["label"]] = TrainStatus(ln["label"], "取得できず", "路線コード不明", None)
            continue
        if key in troubled:
            t = troubled[key]
            texts = [str(v) for v in _flatten_strings(t)]
            out[ln["label"]] = TrainStatus(ln["label"], "運行情報あり", " ".join(texts)[:120], False)
        else:
            out[ln["label"]] = TrainStatus(ln["label"], "平常運転", "", True)
    return out


def _flatten_strings(obj):
    if isinstance(obj, str):
        if obj.strip():
            yield obj.strip()
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_strings(v)


def fetch_yahoo(lines: list[dict], dump_dir: Path | None) -> dict[str, TrainStatus]:
    """Yahoo!路線情報 エリア一覧ページを解析。HTML構造が変わると「取得できず」になる。"""
    page = http_get(CONFIG["yahoo_area_url"]).decode("utf-8", errors="replace")
    if dump_dir:
        (dump_dir / "yahoo_area.html").write_text(page, encoding="utf-8")

    out: dict[str, TrainStatus] = {}
    m = re.search(r'id="mdStatusTroubleLine"(.*?)</table>', page, re.S)
    no_trouble = "事故・遅延に関する情報はありません" in page
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S) if m else []
    parsed = []
    for r in rows:
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if cells:
            parsed.append(cells)

    for ln in lines:
        hits = [c for c in parsed if ln["match"] in c[0]]
        if hits:
            status = hits[0][1] if len(hits[0]) > 1 else "運行情報あり"
            details = [f"{c[0]}: {c[2]}" if len(c) > 2 else c[0] for c in hits]
            out[ln["label"]] = TrainStatus(ln["label"], status, " / ".join(details)[:160], False)
        elif m or no_trouble:
            out[ln["label"]] = TrainStatus(ln["label"], "平常運転", "", True)
        else:
            # 構造が想定と違う: 平常と誤表示しない
            out[ln["label"]] = TrainStatus(ln["label"], "取得できず", "ページ構造が想定外", None)
    return out


def fetch_trains(dump_dir: Path | None) -> list[TrainStatus]:
    results: dict[str, TrainStatus] = {}
    by_source: dict[str, list[dict]] = {}
    for ln in CONFIG["train_lines"]:
        by_source.setdefault(ln["source"], []).append(ln)
    fetchers = {"jrwest": fetch_jrwest, "yahoo": fetch_yahoo}
    for src, lns in by_source.items():
        try:
            results.update(fetchers[src](lns, dump_dir))
        except Exception as e:  # noqa: BLE001
            for ln in lns:
                results[ln["label"]] = TrainStatus(ln["label"], "取得できず", type(e).__name__, None)
    return [results[ln["label"]] for ln in CONFIG["train_lines"]]


def fetch_news(dump_dir: Path | None) -> list[str]:
    raw = http_get(CONFIG["news_rss"])
    if dump_dir:
        (dump_dir / "news.xml").write_bytes(raw)
    root = ET.fromstring(raw)
    titles = [i.findtext("title", "").strip() for i in root.iter("item")]
    return [t for t in titles if t][: CONFIG["news_count"]]


# ---- 1時間降水量(予報) --------------------------------------------
@dataclass
class HourlyRain:
    hours: list[int]       # 時(0-23)
    mm: list[float]        # 1時間降水量 mm


def fetch_rain(dump_dir: Path | None) -> HourlyRain:
    """Open-Meteo の1時間降水量予報。日本域では気象庁モデルを含む合成予報になる。"""
    lat, lon = CONFIG["lat_lon"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}&hourly=precipitation"
           "&timezone=Asia%2FTokyo&forecast_days=2")
    raw = http_get(url)
    if dump_dir:
        (dump_dir / "openmeteo.json").write_bytes(raw)
    j = json.loads(raw)
    times = j["hourly"]["time"]
    vals = j["hourly"]["precipitation"]
    now = dt.datetime.now(JST).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hours, mm = [], []
    for t, v in zip(times, vals):
        tm = dt.datetime.fromisoformat(t)
        if tm >= now and len(hours) < CONFIG["rain_hours"]:
            hours.append(tm.hour)
            mm.append(float(v or 0))
    if not hours:
        raise ValueError("予報データが空")
    return HourlyRain(hours, mm)


# ---- 市況 ------------------------------------------------------------
@dataclass
class Quote:
    label: str
    value: float | None
    prev: float | None
    kind: str = "index"  # index / fx / yield / usd

    @property
    def change(self):
        if self.value is None or self.prev is None:
            return None
        return self.value - self.prev


def _yahoo_chart(symbol: str) -> tuple[float, float]:
    """Yahoo Finance の chart エンドポイント(非公式)。終値ベースで直近2本を返す。"""
    from urllib.parse import quote
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=5d&interval=1d"
    j = json.loads(http_get(url))
    res = j["chart"]["result"][0]
    meta = res["meta"]
    price = meta.get("regularMarketPrice")
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    if price is None:
        price = closes[-1]
    # 当日分の足がまだ無いときは最後の終値が前日終値
    prev = closes[-2] if len(closes) >= 2 and abs(closes[-1] - price) < 1e-9 else closes[-1]
    return float(price), float(prev)


def _stooq_daily(symbol: str) -> tuple[float, float]:
    """Stooq の日足CSV。最後の2行の終値を使う。"""
    from urllib.parse import quote
    url = f"https://stooq.com/q/d/l/?s={quote(symbol)}&i=d"
    rows = http_get(url).decode("utf-8", errors="replace").strip().splitlines()
    if len(rows) < 3 or not rows[0].lower().startswith("date"):
        raise ValueError("stooq: 想定外の応答")
    last = rows[-1].split(",")
    prev = rows[-2].split(",")
    return float(last[4]), float(prev[4])


PROVIDERS = {"yahoo": _yahoo_chart, "stooq": _stooq_daily}


def fetch_markets(dump_dir: Path | None) -> list[Quote]:
    out = []
    for m in CONFIG["markets"]:
        q = Quote(m["label"], None, None, m.get("kind", "index"))
        errs = []
        for prov, sym in m["sources"]:  # 上から順に試し、取れた時点で終了
            try:
                q.value, q.prev = PROVIDERS[prov](sym)
                break
            except Exception as e:  # noqa: BLE001
                errs.append(f"{prov}:{sym}:{type(e).__name__}")
        if q.value is None:
            print(f"{m['label']} の取得に失敗:", errs, file=sys.stderr)
        out.append(q)
    if dump_dir:
        (dump_dir / "markets.json").write_text(
            json.dumps([q.__dict__ for q in out], ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _last_slot(now: dt.datetime) -> dt.datetime:
    """直近の更新時刻(現在時刻以前で最も新しいもの)"""
    slots = []
    for day in (now.date() - dt.timedelta(days=1), now.date()):
        for hm in CONFIG["market_update_times"]:
            h, m = map(int, hm.split(":"))
            slots.append(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=JST))
    return max(s for s in slots if s <= now)


def load_markets(cache_path: Path, dump_dir: Path | None, force: bool = False):
    """1日2回だけ取得し、それ以外はキャッシュを返す。戻り値: (quotes, 取得時刻)"""
    now = dt.datetime.now(JST)
    cached, fetched_at = None, None
    try:
        c = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = dt.datetime.fromisoformat(c["fetched_at"])
        cached = [Quote(**q) for q in c["quotes"]]
    except (FileNotFoundError, KeyError, ValueError, TypeError):
        pass
    if not force and cached is not None and fetched_at >= _last_slot(now):
        return cached, fetched_at
    quotes = fetch_markets(dump_dir)
    if all(q.value is None for q in quotes) and cached is not None:
        return cached, fetched_at  # 全滅なら古い値を残す
    cache_path.write_text(json.dumps(
        {"fetched_at": now.isoformat(), "quotes": [q.__dict__ for q in quotes]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return quotes, now


def fmt_quote(q: Quote) -> tuple[str, str]:
    """(値, 前日比) の表示文字列"""
    if q.value is None:
        return "取得できず", ""
    c = q.change
    sign = lambda x: "▲" if x > 0 else "▼" if x < 0 else "±"  # noqa: E731
    if q.kind == "fx":
        v = f"{q.value:.2f}"
        ch = f"{sign(c)}{abs(c):.2f}" if c is not None else ""
    elif q.kind == "yield":
        v = f"{q.value:.3f}%"
        ch = f"{sign(c)}{abs(c) * 100:.1f}bp" if c is not None else ""
    elif q.kind == "usd":
        v = f"${q.value:.2f}"
        ch = f"{sign(c)}{abs(c) / q.prev * 100:.2f}%" if c is not None and q.prev else ""
    else:
        v = f"{q.value:,.2f}"
        ch = f"{sign(c)}{abs(c) / q.prev * 100:.2f}%" if c is not None and q.prev else ""
    return v, ch


# ------------------------------------------------------------------
# サンプルデータ
# ------------------------------------------------------------------
def mock_data():
    today = dt.datetime.now(JST).date()
    codes = ["201", "101", "300", "200", "101", "100", "202"]
    texts = ["くもり昼過ぎから晴れ", "晴れ時々くもり", "雨", "くもり", "晴れ時々くもり", "晴れ", "くもり一時雨"]
    days = []
    for i in range(7):
        days.append(DayWeather(
            date=today + dt.timedelta(days=i), code=codes[i], text=texts[i],
            tmax=str(22 - i % 3), tmin=str(13 + i % 2) if i else "",
            pops=(["", "10", "0", "0"] if i == 0 else ["10", "20", "10", "10"] if i == 1 else []),
            pop=str([10, 20, 70, 40, 20, 10, 50][i]),
        ))
    trains = [
        TrainStatus("JR西日本(近畿)", "遅れあり", "大阪環状線: 17:40頃、天王寺駅での安全確認の影響で、一部列車に遅れが出ています。", False),
        TrainStatus("御堂筋線", "平常運転", "", True),
        TrainStatus("谷町線", "平常運転", "", True),
        TrainStatus("京阪電車", "平常運転", "", True),
    ]
    news = [
        "（サンプル）衆議院予算委員会で補正予算案の審議始まる",
        "（サンプル）大阪・関西で今季一番の冷え込み 各地で最低気温10度下回る",
        "（サンプル）日経平均株価 終値は前日比で値上がり",
        "（サンプル）インフルエンザ患者数 全国で流行の目安超える",
        "（サンプル）プロ野球 日本シリーズ第2戦の結果",
        "（サンプル）新しい紙の保険証の扱い 厚労省が方針示す",
    ]
    rain = HourlyRain(hours=[(dt.datetime.now(JST).hour + i) % 24 for i in range(12)],
                      mm=[0, 0, 0.5, 1.5, 4.0, 2.5, 1.0, 0, 0, 0, 0.5, 0])
    markets = [
        Quote("ドル円", 152.48, 152.11, "fx"), Quote("日経平均", 39_812.55, 39_605.10),
        Quote("TOPIX", 2_745.31, 2_751.02), Quote("ダウ", 43_120.64, 42_980.40),
        Quote("NASDAQ", 19_210.73, 19_351.20), Quote("WTI原油", 71.84, 72.30, "usd"),
        Quote("米10年債", 4.218, 4.185, "yield"),
    ]
    return Weather(days=days), trains, news, rain, markets, _last_slot(dt.datetime.now(JST))


# ------------------------------------------------------------------
# 描画
# ------------------------------------------------------------------
FONT_CANDIDATES = [
    "C:/Windows/Fonts/BIZ-UDGothicB.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
]
FONT_CANDIDATES_REG = [
    "C:/Windows/Fonts/BIZ-UDGothicR.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
]


def find_font(cands: list[str], override: str | None) -> str:
    if override and os.path.exists(override):
        return override
    for c in cands:
        if os.path.exists(c):
            return c
    sys.exit("日本語フォントが見つかりません。--font でパスを指定してください。")


class Painter:
    def __init__(self, size, bold_path, reg_path):
        self.img = Image.new("L", size, 255)
        self.d = ImageDraw.Draw(self.img)
        self.W, self.H = size
        self._bold, self._reg = bold_path, reg_path
        self._cache = {}

    def f(self, px: int, bold=True):
        k = (px, bold)
        if k not in self._cache:
            idx = 0
            path = self._bold if bold else self._reg
            # Noto CJK の ttc は index 0 が JP
            self._cache[k] = ImageFont.truetype(path, px, index=idx)
        return self._cache[k]

    def text(self, xy, s, px, bold=True, fill=0, anchor="la"):
        self.d.text(xy, s, font=self.f(px, bold), fill=fill, anchor=anchor)

    def tw(self, s, px, bold=True):
        return self.d.textlength(s, font=self.f(px, bold))

    def wrap(self, s: str, px: int, width: int, bold=False, max_lines=2) -> list[str]:
        lines, cur = [], ""
        for ch in s:
            if self.tw(cur + ch, px, bold) > width:
                lines.append(cur)
                cur = ch
                if len(lines) == max_lines:
                    break
            else:
                cur += ch
        else:
            if cur:
                lines.append(cur)
        if len(lines) == max_lines and "".join(lines) != s:
            last = lines[-1]
            while last and self.tw(last + "…", px, bold) > width:
                last = last[:-1]
            lines[-1] = last + "…"
        return lines

    def hline(self, y, x0=56, x1=None, w=3, fill=0):
        self.d.line([(x0, y), ((x1 or self.W - 56), y)], fill=fill, width=w)


# 天気アイコン(気象庁 weatherCode の百の位: 1晴 2曇 3雨 4雪)
def draw_sun(d, cx, cy, r, fill=0):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fill, width=int(max(3, r // 7)))
    import math
    for k in range(8):
        a = k * math.pi / 4
        d.line([(cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35),
                (cx + math.cos(a) * r * 1.75, cy + math.sin(a) * r * 1.75)],
               fill=fill, width=int(max(3, r // 7)))


def _cloud_mask(size, cx, cy, r, inset=0):
    m = Image.new("L", size, 0)
    md = ImageDraw.Draw(m)
    parts = [(cx - r * 0.55, cy + r * 0.12, r * 0.45), (cx + r * 0.55, cy + r * 0.17, r * 0.4),
             (cx, cy - r * 0.15, r * 0.6)]
    for x, y, rr in parts:
        rr -= inset
        md.ellipse([x - rr, y - rr, x + rr, y + rr], fill=255)
    md.rounded_rectangle([cx - r * 0.95 + inset, cy + r * 0.1 + inset, cx + r * 0.95 - inset, cy + r * 0.57 - inset],
                         radius=max(1, int(r * 0.22 - inset)), fill=255)
    return m


def draw_cloud(d, cx, cy, r, fill=0, bg=255):
    """円と角丸矩形の和集合を作り、内側を白・縁を黒で描く(重なっても線が残らない)"""
    img = d._image
    w = int(max(3, r // 7))
    outer = _cloud_mask(img.size, cx, cy, r)
    inner = _cloud_mask(img.size, cx, cy, r, inset=w)
    img.paste(fill, mask=outer)
    img.paste(bg, mask=inner)


def draw_rain(d, cx, cy, r, fill=0):
    w = int(max(3, r // 7))
    for k in (-1, 0, 1):
        x = cx + k * r * 0.4
        d.line([(x, cy + r * 0.75), (x - r * 0.15, cy + r * 1.1)], fill=fill, width=w)


def draw_snow(d, cx, cy, r, fill=0):
    for k in (-1, 0, 1):
        x = cx + k * r * 0.4
        y = cy + r * 0.95
        rr = int(max(3, r // 9))
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=fill)


def draw_weather_icon(d, code: str, cx, cy, r):
    head = code[:1] if code else "2"
    sub = code[1:2] if len(code) > 1 else "0"
    if head == "1":
        if code == "100":
            draw_sun(d, cx, cy, r * 0.62)
        else:  # 晴れ時々くもり など
            draw_sun(d, cx - r * 0.3, cy - r * 0.3, r * 0.42)
            draw_cloud(d, cx + r * 0.2, cy + r * 0.1, r * 0.72)
    elif head == "2":
        if code in ("201", "210", "211"):  # くもり時々(のち)晴れ
            draw_sun(d, cx - r * 0.3, cy - r * 0.3, r * 0.42)
            draw_cloud(d, cx + r * 0.2, cy + r * 0.1, r * 0.72)
        else:
            draw_cloud(d, cx, cy - r * 0.05, r)
    elif head == "3":
        draw_cloud(d, cx, cy - r * 0.2, r)
        draw_rain(d, cx, cy - r * 0.2, r)
    elif head == "4":
        draw_cloud(d, cx, cy - r * 0.2, r)
        draw_snow(d, cx, cy - r * 0.2, r)
    else:
        draw_cloud(d, cx, cy, r)


def render(weather: Weather | None, trains: list[TrainStatus], news: list[str] | None,
           rain: HourlyRain | None, markets: list[Quote] | None, markets_at: dt.datetime | None,
           errors: list[str], bold: str, reg: str) -> Image.Image:
    """横長(1648x1236)レイアウト。
    ┌ 日付 ──────────────────── 更新・地名 ┐
    │ 今日 │ 明日      │ 運行情報(4路線)     │
    │ 週間予報         │ 1時間降水量         │
    ├ ニュース ────────┼ 市況 ───────────┤
    └ 出典/エラー ──────────────────────┘
    """
    p = Painter(CONFIG["size"], bold, reg)
    d = p.d
    W, H = p.W, p.H
    M = 52
    now = dt.datetime.now(JST)

    # ---- ヘッダー
    y = 36
    p.text((M, y), f"{now.month}月{now.day}日", 88)
    date_w = p.tw(f"{now.month}月{now.day}日", 88)
    p.text((M + date_w + 12, y + 28), f"（{WEEKDAYS[now.weekday()]}）", 52)
    p.text((W - M, y + 14), f"更新 {now:%H:%M}", 34, bold=False, anchor="ra")
    p.text((W - M, y + 58), CONFIG["place_label"], 34, bold=False, anchor="ra")
    y = 150
    p.hline(y, x0=M, x1=W - M, w=4)

    top = y + 24
    split_x = 1000  # 左: 天気 / 右: 運行情報
    left_w = split_x - M - 30

    # ---- 天気(今日・明日)
    col_w = left_w // 2
    if weather and weather.days:
        for i, label in enumerate(["今日", "明日"]):
            if i >= len(weather.days):
                break
            day = weather.days[i]
            x0 = M + i * (col_w + 30)
            yy = top
            p.text((x0, yy), label, 38)
            draw_weather_icon(d, day.code, x0 + 80, yy + 128, 66)
            tx = x0 + 186
            p.text((tx, yy + 50), f"{day.tmax or '-'}°", 88)
            if day.tmin:
                p.text((tx + 6, yy + 152), f"最低 {day.tmin}°", 32, bold=False)
            for k, line in enumerate(p.wrap(day.text or "", 30, col_w - 10, bold=False, max_lines=2)):
                p.text((x0, yy + 210 + k * 40), line, 30, bold=False)
            labels = ["0-6", "6-12", "12-18", "18-24"]
            pops = day.pops
            if len(pops) < 4:  # 今日は過ぎた時間帯が欠ける
                pops = [""] * (4 - len(pops)) + pops
            cw = col_w / 4
            py = yy + 300
            p.text((x0, py), "降水確率", 24, bold=False, fill=60)
            for k in range(4):
                cx = x0 + cw * k + cw / 2
                p.text((cx, py + 32), labels[k], 22, bold=False, fill=90, anchor="ma")
                v = pops[k]
                p.text((cx, py + 60), f"{v}%" if v else "-", 32, anchor="ma")
        mid = M + col_w + 15
        d.line([(mid, top + 6), (mid, top + 400)], fill=150, width=2)
    else:
        p.text((M, top + 40), "天気を取得できませんでした", 40)

    # ---- 週間(3日目以降)
    wy = top + 420
    d.line([(M, wy), (split_x - 30, wy)], fill=120, width=2)
    wy += 16
    if weather and len(weather.days) > 2:
        week = weather.days[2:7]
        cw = left_w / len(week)
        for k, day in enumerate(week):
            cx = M + cw * k + cw / 2
            p.text((cx, wy), f"{day.date.day}({WEEKDAYS[day.date.weekday()]})", 28, anchor="ma")
            draw_weather_icon(d, day.code, cx, wy + 78, 30)
            p.text((cx, wy + 118), f"{day.tmax or '-'}/{day.tmin or '-'}", 28, anchor="ma")
            p.text((cx, wy + 154), f"{day.pop}%" if day.pop else "-", 24, bold=False, anchor="ma", fill=70)
    band_bottom = wy + 196

    # ---- 縦の区切り
    d.line([(split_x, top - 4), (split_x, band_bottom)], fill=0, width=3)

    # ---- 運行情報(右カラム)
    rx0, rx1 = split_x + 34, W - M
    rain_top = band_bottom - 206  # 降水量グラフは週間予報と同じ高さに置く
    ry = top
    p.text((rx0, ry), "運行情報", 38)
    ry += 64
    for t in trains:
        p.text((rx0, ry + 8), t.label, 34)
        badge = t.status
        bw = p.tw(badge, 28) + 32
        bx0 = rx1 - bw
        if t.ok is False:  # 異常は黒地白文字
            d.rounded_rectangle([bx0, ry, rx1, ry + 50], radius=10, fill=0)
            p.text(((bx0 + rx1) / 2, ry + 25), badge, 28, fill=255, anchor="mm")
        else:
            d.rounded_rectangle([bx0, ry, rx1, ry + 50], radius=10, outline=0 if t.ok else 140, width=3)
            p.text(((bx0 + rx1) / 2, ry + 25), badge, 28, fill=0 if t.ok else 110, anchor="mm")
        ry += 64
        if t.detail and t.ok is False:
            for line in p.wrap(t.detail, 24, rx1 - rx0 - 16, max_lines=2):
                p.text((rx0 + 16, ry), line, 24, bold=False, fill=40)
                ry += 32
            ry += 10

    # ---- 1時間降水量(右カラム下)
    d.line([(rx0, rain_top - 14), (rx1, rain_top - 14)], fill=120, width=2)
    p.text((rx0, rain_top), "1時間降水量", 30)
    p.text((rx1, rain_top + 6), "mm／今後12時間", 22, bold=False, fill=90, anchor="ra")
    gy0, gy1 = rain_top + 56, band_bottom - 40  # グラフ領域(上端, 基線)
    if rain:
        n = len(rain.mm)
        gw = (rx1 - rx0) / n
        vmax = max(4.0, max(rain.mm))  # 4mm未満の雨でも棒が潰れないよう下限を設ける
        for k, (h, v) in enumerate(zip(rain.hours, rain.mm)):
            bx = rx0 + gw * k
            bh = (gy1 - gy0 - 26) * v / vmax
            if v > 0:
                d.rectangle([bx + 5, gy1 - bh, bx + gw - 5, gy1], fill=0 if v >= 1 else 120)
                p.text((bx + gw / 2, gy1 - bh - 4), f"{v:g}", 20, anchor="md")
            if k % 3 == 0:
                p.text((bx + gw / 2, gy1 + 6), f"{h}時", 20, bold=False, fill=70, anchor="ma")
        d.line([(rx0, gy1), (rx1, gy1)], fill=0, width=2)
        if max(rain.mm) == 0:
            p.text(((rx0 + rx1) / 2, (gy0 + gy1) / 2), "雨の予報なし", 28, bold=False, fill=90, anchor="mm")
    else:
        p.text((rx0, gy0 + 20), "取得できず", 28, bold=False, fill=110)

    # ---- ニュース(下段左)
    y = band_bottom + 4
    p.hline(y, x0=M, x1=W - M, w=4)
    d.line([(split_x, y), (split_x, H - 62)], fill=0, width=3)
    y += 20
    bottom_y = y
    nx1 = split_x - 30
    p.text((M, y), "ニュース", 36)
    p.text((nx1, y + 8), "NHK", 24, bold=False, fill=110, anchor="ra")
    y += 56
    footer_top = H - 62
    if news:
        for title in news:
            lines = p.wrap(title, 28, nx1 - M - 30, bold=False, max_lines=1)
            if y + 42 > footer_top:
                break
            d.rectangle([M, y + 12, M + 9, y + 21], fill=0)
            p.text((M + 28, y), lines[0], 28, bold=False)
            y += 44
    else:
        p.text((M, y), "ニュースを取得できませんでした", 30, bold=False)

    # ---- 市況(下段右)
    my = bottom_y
    p.text((rx0, my), "市況", 36)
    stamp = f"{markets_at.month}/{markets_at.day} {markets_at:%H:%M}時点・前日比" if markets_at else "前日比"
    p.text((rx1, my + 8), stamp, 22, bold=False, fill=110, anchor="ra")
    my += 54
    if markets:
        row_h = min(40, (footer_top - my) / max(1, len(markets)))
        for q in markets:
            val, ch = fmt_quote(q)
            p.text((rx0, my), q.label, 26, bold=False)
            p.text((rx0 + 330, my), val, 28, anchor="ra")
            p.text((rx1, my), ch, 26, bold=(ch[:1] == "▼"), anchor="ra", fill=0)
            my += row_h
    else:
        p.text((rx0, my), "取得できず", 28, bold=False, fill=110)

    # ---- フッター
    p.hline(H - 52, x0=M, x1=W - M, w=1, fill=160)
    foot = "天気: 気象庁 / Open-Meteo  鉄道: JR西日本 / Yahoo!路線情報  ニュース: NHK  市況: Yahoo Finance / Stooq（遅延あり）"
    if errors:
        foot = "取得エラー: " + ", ".join(errors)
    foot = p.wrap(foot, 22, W - 2 * M, max_lines=1)[0]
    p.text((M, H - 40), foot, 22, bold=False, fill=90)
    return p.img


# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="サンプルデータで描画")
    ap.add_argument("--dump", action="store_true", help="取得した生データを out/raw に保存")
    ap.add_argument("--out", default="out/dashboard.png")
    ap.add_argument("--font", help="太字フォントのパス")
    ap.add_argument("--font-regular", help="通常フォントのパス")
    ap.add_argument("--refresh-markets", action="store_true", help="更新時刻に関係なく市況を取り直す")
    ap.add_argument("--rotate", type=int, choices=[0, 90, 180, 270],
                    help="Kindle 用に回転する角度(既定は CONFIG['rotate'])")
    args = ap.parse_args()

    bold = find_font(FONT_CANDIDATES, args.font)
    reg = find_font(FONT_CANDIDATES_REG, args.font_regular) if not args.font_regular else args.font_regular

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_dir = None
    if args.dump:
        dump_dir = out.parent / "raw"
        dump_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    if args.mock:
        weather, trains, news, rain, markets, markets_at = mock_data()
    else:
        try:
            weather = fetch_weather(dump_dir)
        except Exception as e:  # noqa: BLE001
            weather = None
            errors.append(f"天気({type(e).__name__})")
            print("天気の取得に失敗:", repr(e), file=sys.stderr)
        trains = fetch_trains(dump_dir)
        for t in trains:
            if t.ok is None:
                errors.append(f"{t.label}({t.detail})")
        try:
            news = fetch_news(dump_dir)
        except Exception as e:  # noqa: BLE001
            news = None
            errors.append(f"ニュース({type(e).__name__})")
            print("ニュースの取得に失敗:", repr(e), file=sys.stderr)
        try:
            rain = fetch_rain(dump_dir)
        except Exception as e:  # noqa: BLE001
            rain = None
            errors.append(f"降水量({type(e).__name__})")
            print("降水量の取得に失敗:", repr(e), file=sys.stderr)
        markets, markets_at = load_markets(out.parent / "markets_cache.json", dump_dir, args.refresh_markets)
        missing = [q.label for q in markets if q.value is None]
        if missing:
            errors.append("市況(" + "・".join(missing) + ")")

    img = render(weather, trains, news, rain, markets, markets_at, errors, bold, reg)
    # Kindleは16階調。量子化してから保存するとe-inkで滲みにくい
    img = img.point(lambda v: round(v / 17) * 17)
    # 横向きのプレビューも残す(確認用)
    img.save(out.with_name(out.stem + "_preview.png"), optimize=True)
    rot = args.rotate if args.rotate is not None else CONFIG["rotate"]
    if rot:
        img = img.rotate(rot, expand=True)  # Kindle の縦長フレームバッファに合わせる
    img.save(out, optimize=True)
    print(f"保存しました: {out}")
    if errors:
        print("一部取得できませんでした:", ", ".join(errors), file=sys.stderr)


if __name__ == "__main__":
    main()
