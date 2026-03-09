# BootAnimation.zip-PREVIEWER V2
Androidのbootanimation.zipをプレビュー再生できるPythonスクリプト

(不具合が多いため現在はベータ版です。)
(動かすことは可能ですがバグを見つけたらhigh28に教えてください。)

# 機能
Partの詳細表示
desc.txtの読み込み
再生、一時停止、再生位置変更
trim.txtの読み込み
FPSの変更
端末ごとの解像度の変更(チャレパに最適化済み)
ドラッグアンドドロップでの再生
キャッシュ生成でスムーズな再生
MP4出力
audio.wav,mp3の読み込み

# インストール方法
### 1. Pythonライブラリのインストール
コマンドプロンプトやターミナルを開き、以下の `pip` コマンドを実行して必要なライブラリをインストールしてください。

```bash
pip install PySide6 Pillow
```
- **PySide6**: UI（ウィンドウやボタン）と、動画・音声プレイヤー（QtMultimedia）を動かすために使います。
- **Pillow**: 画像の合成やリサイズ処理（PIL）を行うために使います。

---

### 2. FFmpeg のインストール（必須）
このアプリは「画像のMP4化」や「音声と動画の書き出し」を行うため、裏側で **FFmpeg（エフエフエムペグ）** というツールを利用しています。PCにインストールされていない場合は、以下の方法でインストールしてください。

#### Windows の場合
一番簡単なのは、コマンドプロンプトやPowerShellで `winget` コマンドを使う方法です。
```cmd
winget install ffmpeg
```
※インストール後、**一度パソコンを再起動**するか、コマンドプロンプトを開き直すことで設定（PATH）が反映されます。

（手動でインストールする場合は、[FFmpegの公式サイト](https://ffmpeg.org/download.html) からWindows向けのzipをダウンロードし、解凍したフォルダ内の `bin` フォルダのパスを環境変数に設定してください。）

#### macOS の場合
Homebrew を使ってインストールするのが簡単です。ターミナルで以下を実行します。
```bash
brew install ffmpeg
```

#### Linux (Ubuntu / Debian系) の場合
ターミナルで以下を実行します。
```bash
sudo apt update
sudo apt install ffmpeg
```

---

### 3. インストールの確認
すべて完了したら、コマンドプロンプト（ターミナル）で以下のコマンドを入力して確認してみてください。

```bash
ffmpeg -version
```
バージョン情報がズラッと表示されれば、正常に認識されています。これで先ほどのPythonスクリプトを実行すれば、正常に動作します！

# 実行方法
bootanimpreviewer.pyがあるパスでcmdを開き、
cmdにて
```python bootanimpreviewer.py```
を実行するだけ。
# 二次配布、改変しての配布、自作発言は禁止です。
