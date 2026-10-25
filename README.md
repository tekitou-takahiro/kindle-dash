# Kindle ダッシュボード画像生成（ステップ1）

Kindle Paperwhite 5 Signature Edition（1236×1648、16階調）向けに、
天気・鉄道運行情報・ニュースを1枚のPNGにまとめるスクリプト。

横向き（1648×1236）で描画し、Kindleの縦長画面に合わせて回転して保存する。
- `out/dashboard.png`：Kindleに表示する画像（回転済み）
- `out/dashboard_preview.png`：確認用の横向き画像

本体を置く向きが逆なら `--rotate 90` を付ける（既定は270）。

## 動かし方（Windows）

```
cd kindle_dash
python -m pip install -r requirements.txt
python dashboard.py --mock      # サンプルデータでレイアウト確認 → out/dashboard.png
python dashboard.py --dump      # 実データで生成し、生データを out/raw/ に保存
```

フォントは BIZ UDゴシック → 游ゴシック → メイリオ の順に自動で探す。
見つからない場合は `--font C:/Windows/Fonts/xxx.ttc` で指定する。

## データの取得元と確実性

| 項目 | 取得元 | 状態 |
|---|---|---|
| 天気 | 気象庁 `bosai/forecast/data/forecast/270000.json`（大阪府、気温は大阪地点 62078） | 気象庁サイトの内部JSON。公式APIとして公開されたものではなく、仕様変更の可能性あり |
| JR西日本（近畿全体） | JR西日本「列車走行位置」の内部JSON（`area_kinki_trafficinfo.json`） | 非公式。遅れのある路線名を `area_kinki_master.json` から引いて列挙 |
| 御堂筋線・谷町線・京阪 | Yahoo!路線情報 近畿エリアページのHTML解析 | **最も壊れやすい部分**。エリアID(6)とHTML構造は未検証 |
| ニュース | NHK 主要ニュース RSS（`cat0.xml`） | 個人利用の範囲で使用 |
| 1時間降水量 | Open-Meteo（東住吉区役所付近の座標、今後12時間） | 無料・登録不要。非商用利用が条件 |
| 市況 | Yahoo Finance chart（非公式）→ 失敗時 Stooq 日足CSV。TOPIX は Stooq 優先 | 非公式・遅延あり。前日比は直近2本の終値から計算 |

取得に失敗した路線は「平常運転」ではなく「取得できず」と表示する。
平常と誤って表示しないことを優先している。

## 市況の更新頻度

市況は1日2回（7:00と16:00）だけ取得し、`out/markets_cache.json` に保存する。
それ以外の実行時はこのキャッシュを表示する。時刻は `CONFIG["market_update_times"]` で変更できる。
すぐに取り直したいときは `--refresh-markets` を付けて実行する。

## うまくいかないとき

`python dashboard.py --dump` を実行し、`out/raw/` にできたファイルと
表示されたエラーメッセージを共有すれば、解析部分を実データに合わせて直せる。

## 設定

`dashboard.py` 冒頭の `CONFIG` を編集する。
- `train_lines`：表示する路線（label=表示名、match=照合文字列、source=jrwest/yahoo）
- `news_count`：ニュースの本数
- `place_label`：右上の地名
