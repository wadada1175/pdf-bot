# 研究室プリンター Discord Bot

Discord の `/print` に PDF を添付すると、研究室の EPSON LP-S440DN で印刷する Bot。
印刷するには、**研究室 LAN に接続した端末で**確認リンクを開いて「印刷する」を押す必要がある。
研究室の Mac mini で動かす。

## しくみ

1. `/print file:<PDF> copies:<部数>` を実行する
2. Bot が PDF を検査（PDF か・サイズ・ページ数・1 日の上限）して、本人にだけ見える確認リンクを返す
3. 研究室 LAN 内の端末でリンクを開き「印刷する」を押す
   - 確認ページは Mac mini 上にある。アクセス元の IP が `LAN_CIDRS` に入っていなければ拒否する
   - LAN 内の IP（192.168.x.x など）なので、そもそも外からは届かない
4. macOS 標準の `lp` コマンドでプリンターに送り、結果を Discord で通知する

確認リンクは 10 分で失効する。印刷ログは `prints.db`（SQLite）に残る。

## セットアップ（研究室の Mac mini）

コマンドはすべて「ターミナル」アプリで実行する。

### 1. フォルダを置く

`print-bot` フォルダをホーム（例: `/Users/<名前>/print-bot`）に置いて移動する。

```bash
cd ~/print-bot
```

### 2. プリンター

1. 「システム設定 → プリンタとスキャナ → プリンタ、スキャナ、またはファクスを追加」で LP-S440DN を追加する
   - 一覧に出ない場合は「IP」タブでプリンターの IP を入れる。EPSON のサイトから macOS 用ドライバーを入れると両面印刷の設定が確実
2. プリンター名を確認する（`.env` の `PRINTER_NAME` に使う）

```bash
lpstat -p
```

`printer EPSON_LP_S440DN is idle.` のように出る。この `EPSON_LP_S440DN` の部分がプリンター名。

3. コマンドで印刷できるか試す（手元の PDF のパスに置き換える）

```bash
lp -d EPSON_LP_S440DN -o sides=two-sided-long-edge ~/Desktop/test.pdf
```

### 3. Python

```bash
python3 --version
```

3.9 以上なら OK。「コマンドライン・デベロッパツールをインストールしますか？」と出たら「インストール」を押し、終わったらもう一度実行する。

```bash
python3 -m venv .venv
```
```bash
.venv/bin/pip install -r requirements.txt
```

### 4. Discord の Bot

1. <https://discord.com/developers/applications> で New Application → Bot → Reset Token でトークンを取得する
2. OAuth2 → URL Generator で scope に `bot` と `applications.commands`、権限に `Send Messages` を選んで、表示された URL から研究室サーバーに招待する
3. Discord の設定で開発者モードをオンにして、サーバー ID・ロール ID・チャンネル ID を右クリックでコピーする

### 5. 設定

```bash
cp .env.example .env
```
```bash
open -e .env
```

テキストエディットが開くので値を埋める。

- `LAN_CIDRS` と `PUBLIC_BASE_URL`: 次で Mac mini の IP を調べる（Wi-Fi なら `en1` のこともある）

```bash
ipconfig getifaddr en0
```

`192.168.1.50` と出たら `LAN_CIDRS=192.168.1.0/24`、`PUBLIC_BASE_URL=http://192.168.1.50:8080`。
IP が変わるとリンクが壊れるので、ルーターか管理者に頼んで IP を固定する。

> 大学全体のネットワークなど、研究室外の端末も同じアドレス範囲に入る場合は、`LAN_CIDRS` を研究室の範囲だけに絞れるか管理者に確認する。

### 6. 起動して試す

```bash
.venv/bin/python bot.py
```

- 「受信接続を許可しますか？」と出たら「許可」を押す
- Discord で Bot がオンラインになったら、研究室 Wi-Fi のスマホから `/print` を試し、確認リンクで印刷できることを確認する
- 止めるときは Ctrl+C

### 7. 自動起動

動作を確認できたら、ログイン時に自動で起動し、落ちても再起動するように登録する。

```bash
bash install-autostart.sh
```

ログは `bot.log` に出る。解除するときは次を実行する。

```bash
launchctl bootout gui/$(id -u)/lab.printbot
```

あわせて次の設定もしておく。

- 「システム設定 → エネルギー」で「ディスプレイがオフのときに自動でスリープさせない」をオン
- 停電後も動くように「停電後に自動的に起動」をオン、「ユーザとグループ」で自動ログインを設定

## コマンド

| コマンド | 内容 |
|---|---|
| `/print file copies` | PDF を片面で印刷する（部数は 1〜5） |
| `/quota` | 今日の印刷ページ数と残りを確認する |

## 制限（`.env` で変更可）

| 項目 | 既定値 |
|---|---|
| ファイルサイズ | 20 MB |
| ページ数 | 50 ページ |
| 部数 | 5 部 |
| 1 人 1 日のページ数 | 100 ページ（ページ数 × 部数） |
| 1 人あたりの確認待ち | 3 件 |
| 確認リンクの有効期限 | 10 分 |

LP-S440DN はモノクロ機なので、カラー指定はない。
