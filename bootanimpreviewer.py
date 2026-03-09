import sys, os, zipfile, tempfile, shutil, re, io, threading, queue, subprocess, time
from dataclasses import dataclass
from typing import List, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSlider, QListWidget, QMessageBox, QComboBox, QSpinBox, QLineEdit, 
    QGroupBox, QFormLayout, QProgressBar, QFileDialog
)
from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QObject, QEvent
from PySide6.QtGui import QKeySequence
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PIL import Image, ImageDraw

# ---------- イベントフィルター ----------

class KeyPressFilter(QObject):
    def __init__(self, main_win):
        super().__init__(main_win)
        self.main_win = main_win

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space:
            # 入力欄にフォーカスがある場合はスペースキーの本来の動作を優先
            if isinstance(QApplication.focusWidget(), QLineEdit) or isinstance(QApplication.focusWidget(), QSpinBox):
                return False
            self.main_win.toggle_play()
            return True
        return super().eventFilter(obj, event)

# ---------- カスタム VideoWidget (D&D対応) ----------

class DropVideoWidget(QVideoWidget):
    dropped = Signal(str)
    
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.overlay = QLabel("ここでクリックを離してロード", self)
        self.overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 180); color: white; "
            "font-size: 24px; font-weight: bold; border: 4px dashed #ccc;"
        )
        self.overlay.setAlignment(Qt.AlignCenter)
        self.overlay.hide()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            url = e.mimeData().urls()[0].toLocalFile()
            if url.lower().endswith('.zip'):
                e.acceptProposedAction()
                self.overlay.resize(self.size())
                self.overlay.show()

    def dragLeaveEvent(self, e):
        self.overlay.hide()

    def dropEvent(self, e):
        self.overlay.hide()
        path = e.mimeData().urls()[0].toLocalFile()
        self.dropped.emit(path)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not self.overlay.isHidden():
            self.overlay.resize(self.size())

# ---------- ユーティリティ ----------

def parse_trim_file(path: str) -> List[Tuple[int, int]]:
    offsets =[]
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                for line in f:
                    m = re.search(r'(\d+)\s*[x,]\s*(\d+)\s*[\+\s,]\s*(\d+)\s*[\+\s,]\s*(\d+)', line)
                    if m:
                        offsets.append((int(m.group(3)), int(m.group(4))))
                    else:
                        parts = re.findall(r'\d+', line)
                        if len(parts) >= 4:
                            offsets.append((int(parts[2]), int(parts[3])))
        except Exception:
            pass
    return offsets

def hex_to_rgba(hexstr: str):
    s = hexstr.strip().replace("#", "")
    try:
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
        elif len(s) == 8:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    except:
        pass
    return (0, 0, 0, 255)

def render_frame_worker(args):
    try:
        flat_idx, img_path, offset, desc_w, desc_h, bgcolor, dev_w, dev_h, out_dir = args
        img = Image.open(img_path).convert("RGBA")
        
        bg_col = hex_to_rgba(bgcolor)
        canvas = Image.new("RGBA", (desc_w, desc_h), bg_col)
        
        if offset:
            canvas.paste(img, offset, img)
        else:
            pos = ((desc_w - img.width) // 2, (desc_h - img.height) // 2)
            canvas.paste(img, pos, img)
        
        scale = min(dev_w / desc_w, dev_h / desc_h)
        nw, nh = max(1, int(desc_w * scale)), max(1, int(desc_h * scale))
        scaled = canvas.resize((nw, nh), resample=Image.LANCZOS)
        
        final = Image.new("RGBA", (dev_w, dev_h), bg_col)
        final.paste(scaled, ((dev_w - nw)//2, (dev_h - nh)//2))
        
        out_path = os.path.join(out_dir, f"frame_{flat_idx:06d}.png")
        final.save(out_path, format="PNG")
        
        return ("img", flat_idx, out_path, None)
    except Exception as e:
        return ("error", flat_idx, None, str(e))

@dataclass
class PartDef:
    mode: str
    count: int
    delay: int
    folder: str
    bgcolor: str
    offsets: List[Tuple[int, int]]
    has_trim: bool
    audio_file: str  # 見つかった場合はパス、なければNone

class BootAnimationStudio(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BootAnim Previewer - Created By.High28(ふたば)[MP4キャッシュ超高速版]")
        self.resize(1150, 750)
        
        self.temp_dir = None
        self.cache_dir = None
        self.parts: List[PartDef] = []
        self.frame_paths: List[List[str]] =[]
        self.part_start_flat_indices =[]
        
        self.desc_w = 0; self.desc_h = 0; self.base_fps = 30
        self.playing = False
        
        self.render_cache = {}
        self.executor = None
        self.msg_queue = queue.Queue()
        self.render_start_time = 0
        self.audio_path = None  # 全体BGM用
        
        self.preview_mp4_ready = False
        self.timeline =[] # (p_idx, f_idx, loop_idx)
        self.part_start_times = {} # p_idx: [ループごとの開始時間ms]
        self.current_playing_part = -1
        self.last_played_audio_marker = None
        
        self.init_ui()
        
        # UI更新タイマー (キューの消化)
        self.ui_update_timer = QTimer()
        self.ui_update_timer.timeout.connect(self.process_queue)
        self.ui_update_timer.start(50)

    def init_ui(self):
        # メインビュー (MP4再生用 QVideoWidget)
        self.view = DropVideoWidget()
        self.view.setMinimumSize(640, 480)
        self.view.setStyleSheet("background-color: #000; border: 2px solid #555; border-radius: 5px;")
        self.view.dropped.connect(self.load_zip)

        # 動画プレイヤー設定
        self.media_player = QMediaPlayer()
        self.media_player.setVideoOutput(self.view)
        self.media_player.positionChanged.connect(self.on_position_changed)
        self.media_player.mediaStatusChanged.connect(self.on_media_status_changed)

        # 音声プレイヤー設定 (手動追加の全体BGM用 と Part自動読み込み用)
        self.bgm_audio_output = QAudioOutput()
        self.bgm_player = QMediaPlayer()
        self.bgm_player.setAudioOutput(self.bgm_audio_output)
        
        self.part_audio_output = QAudioOutput()
        self.part_audio_player = QMediaPlayer()
        self.part_audio_player.setAudioOutput(self.part_audio_output)

        # コントロール
        self.btn_open_zip = QPushButton("ZIPを開く")
        self.btn_open_zip.clicked.connect(self.open_zip_dialog)
        
        self.btn_play = QPushButton("再生 / 一時停止 (Space)")
        self.btn_play.clicked.connect(self.toggle_play)
        
        self.btn_load_audio = QPushButton("全体BGMを追加 (任意)")
        self.btn_load_audio.clicked.connect(self.load_audio)
        
        self.btn_export_mp4 = QPushButton("MP4エクスポート")
        self.btn_export_mp4.setStyleSheet("background-color: #2e8b57; color: white; font-weight: bold;")
        self.btn_export_mp4.clicked.connect(self.export_mp4)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(self.manual_seek)
        
        self.info_label = QLabel("待機中: ZIPを動画エリアにドラッグ＆ドロップしてください")
        self.info_label.setStyleSheet("font-family: 'Consolas', monospace; font-size: 13px;")
        self.progress_bar = QProgressBar()

        # サイドパネル
        self.side = QVBoxLayout()
        group = QGroupBox("設定 (Configuration)")
        form = QFormLayout()
        
        self.combo_preset = QComboBox()
        self.combo_preset.addItems([
            "オリジナルサイズ (desc.txt)", 
            "チャレンジタッチ 1/2 (800x480)", 
            "チャレンジタッチ 3 (1280x800)", 
            "チャレンジタッチ NEO/NEXT (1920x1200)", 
            "720x1280 (縦画面)",
            "1080x1920 (縦画面)"
        ])
        self.combo_preset.currentTextChanged.connect(self.apply_preset)
        
        self.edit_w = QLineEdit("1280"); self.edit_h = QLineEdit("800")
        self.spin_fps = QSpinBox(); self.spin_fps.setRange(1, 120); self.spin_fps.setValue(30)
        self.spin_fps.valueChanged.connect(self.start_render)
        
        self.btn_apply = QPushButton("適用 & キャッシュ再生成")
        self.btn_apply.clicked.connect(self.start_render)

        form.addRow("プリセット:", self.combo_preset)
        size_layout = QHBoxLayout(); size_layout.addWidget(self.edit_w); size_layout.addWidget(QLabel("x")); size_layout.addWidget(self.edit_h)
        form.addRow("画面サイズ:", size_layout)
        form.addRow("FPS (速度):", self.spin_fps)
        form.addRow(self.btn_apply)
        group.setLayout(form)
        
        # 進捗スライダー (Partごと)
        self.part_progress_label = QLabel("Part 進捗: 0 / 0")
        self.part_progress_label.setStyleSheet("font-weight: bold; color: #2e8b57;")
        self.part_progress_slider = QSlider(Qt.Horizontal)
        self.part_progress_slider.sliderMoved.connect(self.manual_part_seek)

        self.list_parts = QListWidget()
        self.list_parts.itemClicked.connect(self.on_part_clicked)

        self.side.addWidget(group)
        self.side.addWidget(self.part_progress_label)
        self.side.addWidget(self.part_progress_slider)
        self.side.addWidget(QLabel("パーツリスト (クリックで頭出し再生):"))
        self.side.addWidget(self.list_parts)

        layout = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.view, 1)
        
        btns = QHBoxLayout()
        btns.addWidget(self.btn_open_zip)
        btns.addWidget(self.btn_play)
        btns.addWidget(self.btn_load_audio)
        btns.addWidget(self.btn_export_mp4)
        
        left.addLayout(btns)
        left.addWidget(self.slider)
        left.addWidget(self.info_label)
        left.addWidget(self.progress_bar)
        
        layout.addLayout(left, 3)
        layout.addLayout(self.side, 1)
        self.setLayout(layout)

    def open_zip_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "BootAnimation ZIP を開く", "", "ZIP Files (*.zip)")
        if file_path:
            self.load_zip(file_path)

    def load_audio(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "BGMファイルを開く", "", "Audio Files (*.wav *.mp3 *.ogg)")
        if file_path:
            self.audio_path = file_path
            self.bgm_player.setSource(QUrl.fromLocalFile(file_path))
            self.btn_load_audio.setText(f"BGM: {os.path.basename(file_path)}")

    def load_zip(self, zip_path):
        if not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "エラー", "システムに ffmpeg が見つかりません。MP4化には ffmpeg が必須です。")
            return

        if self.temp_dir: shutil.rmtree(self.temp_dir, ignore_errors=True)
        if self.cache_dir: shutil.rmtree(self.cache_dir, ignore_errors=True)
        self.temp_dir = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(self.temp_dir)
            
            desc_path = None
            for root, _, files in os.walk(self.temp_dir):
                if "desc.txt" in files: desc_path = os.path.join(root, "desc.txt"); break

            if not desc_path: raise Exception("desc.txt が見つかりませんでした")

            with open(desc_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines =[l.strip() for l in f.readlines() if l.strip()]

            header = lines[0].split()
            self.desc_w, self.desc_h, self.base_fps = int(header[0]), int(header[1]), int(header[2])
            
            self.edit_w.setText(str(self.desc_w)); self.edit_h.setText(str(self.desc_h))
            self.spin_fps.blockSignals(True)
            self.spin_fps.setValue(self.base_fps)
            self.spin_fps.blockSignals(False)

            self.parts, self.frame_paths, self.part_start_flat_indices = [], [],[]
            self.list_parts.clear()
            
            base_dir = os.path.dirname(desc_path)
            flat_idx_counter = 0

            for i, line in enumerate(lines[1:]):
                p_items = line.split()
                if len(p_items) < 4: continue
                
                mode, count, delay, folder = p_items[0], int(p_items[1]), int(p_items[2]), p_items[3]
                bgcolor = p_items[4] if len(p_items) > 4 else "000000"
                
                f_dir = os.path.join(base_dir, folder)
                if not os.path.exists(f_dir): continue

                frames = sorted([os.path.join(f_dir, f) for f in os.listdir(f_dir) if f.lower().endswith(('.png', '.jpg'))])
                trim_offsets = parse_trim_file(os.path.join(f_dir, "trim.txt"))
                
                # 自動音声読み込み (partディレクトリ内のaudio.wav / audio.mp3)
                part_audio = None
                for ext in ['wav', 'mp3', 'ogg']:
                    aud_path = os.path.join(f_dir, f"audio.{ext}")
                    if os.path.exists(aud_path):
                        part_audio = aud_path
                        break
                
                self.part_start_flat_indices.append(flat_idx_counter)
                flat_idx_counter += len(frames)
                
                self.parts.append(PartDef(mode, count, delay, folder, bgcolor, trim_offsets, len(trim_offsets)>0, part_audio))
                self.frame_paths.append(frames)
                
                aud_str = "🎵あり" if part_audio else "無音"
                loop_str = "無限" if count == 0 else f"{count}回"
                self.list_parts.addItem(f"Part {i}: {folder} ({len(frames)}f)[ループ:{loop_str}] [{aud_str}]")

            self.start_render()

        except Exception as e:
            QMessageBox.critical(self, "読み込みエラー", str(e))

    def start_render(self):
        if not self.parts: return
        self.pause()
        self.preview_mp4_ready = False
        self.render_cache.clear()
        self.progress_bar.setValue(0)
        
        target_w = int(self.edit_w.text()) if self.edit_w.text().isdigit() else self.desc_w
        target_h = int(self.edit_h.text()) if self.edit_h.text().isdigit() else self.desc_h
        
        if self.executor: self.executor.shutdown(wait=False, cancel_futures=True)
        self.executor = ProcessPoolExecutor(max_workers=max(1, os.cpu_count() - 1))
        
        tasks =[]
        for p_idx, frames in enumerate(self.frame_paths):
            start_flat = self.part_start_flat_indices[p_idx]
            part = self.parts[p_idx]
            for f_idx, f_path in enumerate(frames):
                offset = part.offsets[f_idx] if f_idx < len(part.offsets) else None
                tasks.append((start_flat + f_idx, f_path, offset, self.desc_w, self.desc_h, part.bgcolor, target_w, target_h, self.cache_dir))
        
        self.total_to_render = len(tasks)
        if self.total_to_render == 0: return

        self.render_start_time = time.time()
        self.ffmpeg_started = False

        def run_all():
            for fut in as_completed([self.executor.submit(render_frame_worker, t) for t in tasks]):
                self.msg_queue.put(fut.result())
            
            self.msg_queue.put(("status", "動画をエンコード中... (ffmpeg)"))
            mp4_path = self.generate_preview_mp4_sync()
            self.msg_queue.put(("video", mp4_path))

        threading.Thread(target=run_all, daemon=True).start()

    def generate_preview_mp4_sync(self):
        """ 完全に展開された1本のプレビュー用MP4を生成する """
        concat_file = os.path.join(self.cache_dir, "concat.txt")
        fps = self.spin_fps.value()
        duration = 1.0 / fps
        self.timeline =[]
        self.part_start_times = {}
        
        current_time_ms = 0.0
        last_frame_path = None

        with open(concat_file, "w", encoding="utf-8") as f:
            for p_idx, part in enumerate(self.parts):
                self.part_start_times[p_idx] = []
                frames = self.frame_paths[p_idx]
                # 無限ループはプレビュー用に3回展開
                loop_count = part.count if part.count > 0 else 3 
                
                for loop_i in range(loop_count):
                    self.part_start_times[p_idx].append(current_time_ms)
                    for f_idx in range(len(frames)):
                        flat_idx = self.part_start_flat_indices[p_idx] + f_idx
                        frame_path = os.path.join(self.cache_dir, f"frame_{flat_idx:06d}.png")
                        f.write(f"file '{frame_path.replace('\\', '/')}'\n")
                        f.write(f"duration {duration:.6f}\n")
                        self.timeline.append((p_idx, f_idx, loop_i))
                        current_time_ms += (1000.0 / fps)
                        last_frame_path = frame_path
                        
                    if part.delay > 0 and last_frame_path:
                        for _ in range(part.delay):
                            f.write(f"file '{last_frame_path.replace('\\', '/')}'\n")
                            f.write(f"duration {duration:.6f}\n")
                            self.timeline.append((p_idx, len(frames)-1, loop_i))
                            current_time_ms += (1000.0 / fps)

        if last_frame_path:
            with open(concat_file, "a", encoding="utf-8") as f:
                f.write(f"file '{last_frame_path.replace('\\', '/')}'\n")

        mp4_path = os.path.join(self.cache_dir, "preview.mp4")
        cmd =[
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
            "-vf", f"fps={fps}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "1", "-pix_fmt", "yuv420p", mp4_path
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        subprocess.run(cmd, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return mp4_path

    def process_queue(self):
        updated = False
        while not self.msg_queue.empty():
            msg = self.msg_queue.get()
            if msg[0] == "img":
                self.render_cache[msg[1]] = msg[2]
                updated = True
            elif msg[0] == "status":
                self.info_label.setText(msg[1])
            elif msg[0] == "video":
                self.on_preview_mp4_ready(msg[1])
                
        if updated and not self.preview_mp4_ready:
            processed = len(self.render_cache)
            val = int(processed / self.total_to_render * 50) # 画像生成は全体の50%
            self.progress_bar.setValue(val)
            elapsed = time.time() - self.render_start_time
            speed = processed / elapsed if elapsed > 0 else 0
            if speed > 0:
                rem = (self.total_to_render - processed) / speed
                m, s = divmod(int(rem), 60)
                self.info_label.setText(f"画像合成中... {processed}/{self.total_to_render} フレーム (残り約 {m}分{s}秒)")

    def on_preview_mp4_ready(self, mp4_path):
        self.preview_mp4_ready = True
        self.progress_bar.setValue(100)
        self.info_label.setText("動画キャッシュ完了！再生可能です。")
        self.slider.setRange(0, len(self.timeline) - 1)
        self.media_player.setSource(QUrl.fromLocalFile(mp4_path))
        self.play()

    def on_position_changed(self, pos_ms):
        if not self.timeline: return
        fps = self.spin_fps.value()
        idx = int(pos_ms / 1000.0 * fps)
        if idx >= len(self.timeline): idx = len(self.timeline) - 1
        
        p_idx, f_idx, loop_idx = self.timeline[idx]
        
        # UI スライダー更新
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)
        
        # Part進捗更新
        total_f = len(self.frame_paths[p_idx])
        self.part_progress_slider.blockSignals(True)
        self.part_progress_slider.setMaximum(total_f - 1)
        self.part_progress_slider.setValue(f_idx)
        self.part_progress_slider.blockSignals(False)
        self.part_progress_label.setText(f"Part {p_idx} 進捗: {f_idx + 1} / {total_f}")

        # 音声の同期再生とリストハイライト
        current_marker = (p_idx, loop_idx)
        if self.last_played_audio_marker != current_marker:
            self.last_played_audio_marker = current_marker
            self.list_parts.clearSelection()
            self.list_parts.setCurrentRow(p_idx)
            
            aud = self.parts[p_idx].audio_file
            if aud:
                self.part_audio_player.setSource(QUrl.fromLocalFile(aud))
                self.part_audio_player.setPosition(0)
                if self.playing: self.part_audio_player.play()

    def on_media_status_changed(self, status):
        # プレビューMP4が最後まで行ったら最初からループ
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.media_player.setPosition(0)
            self.media_player.play()
            if self.audio_path:
                self.bgm_player.setPosition(0)
                self.bgm_player.play()

    def toggle_play(self):
        if self.playing: self.pause()
        else: self.play()

    def play(self): 
        if not self.preview_mp4_ready: return
        self.playing = True
        self.media_player.play()
        self.part_audio_player.play()
        if self.audio_path: self.bgm_player.play()

    def pause(self): 
        self.playing = False
        self.media_player.pause()
        self.part_audio_player.pause()
        self.bgm_player.pause()

    def manual_seek(self, val):
        if not self.timeline: return
        fps = self.spin_fps.value()
        pos_ms = int(val * 1000.0 / fps)
        self.media_player.setPosition(pos_ms)
        if self.audio_path: self.bgm_player.setPosition(pos_ms)

    def manual_part_seek(self, val):
        if not self.timeline: return
        idx = self.list_parts.currentRow()
        if idx >= 0 and idx in self.part_start_times and self.part_start_times[idx]:
            # 現在のループの開始時間から計算
            loop_idx = self.last_played_audio_marker[1] if self.last_played_audio_marker else 0
            if loop_idx >= len(self.part_start_times[idx]): loop_idx = 0
            base_ms = self.part_start_times[idx][loop_idx]
            pos_ms = int(base_ms + (val * 1000.0 / self.spin_fps.value()))
            self.media_player.setPosition(pos_ms)

    def on_part_clicked(self, item):
        idx = self.list_parts.row(item)
        if idx in self.part_start_times and self.part_start_times[idx]:
            self.last_played_audio_marker = None # 音声も強制リセット
            first_ms = self.part_start_times[idx][0]
            self.media_player.setPosition(int(first_ms))
            if not self.playing: self.toggle_play()

    def apply_preset(self, text):
        presets = {
            "チャレンジタッチ 1/2 (800x480)": (800, 480),
            "チャレンジタッチ 3 (1280x800)": (1280, 800),
            "チャレンジタッチ NEO/NEXT (1920x1200)": (1920, 1200),
            "720x1280 (縦画面)": (720, 1280),
            "1080x1920 (縦画面)": (1080, 1920)
        }
        if text in presets:
            w, h = presets[text]
            self.edit_w.setText(str(w)); self.edit_h.setText(str(h))
            self.start_render()
        elif "オリジナル" in text and self.desc_w > 0:
            self.edit_w.setText(str(self.desc_w)); self.edit_h.setText(str(self.desc_h))
            self.start_render()

    def export_mp4(self):
        if not self.preview_mp4_ready:
            QMessageBox.warning(self, "エラー", "キャッシュ(プレビュー動画)の生成が終わってから実行してください。")
            return
            
        save_path, _ = QFileDialog.getSaveFileName(self, "MP4で保存", "", "MP4 Video (*.mp4)")
        if not save_path: return
            
        self.pause()
        self.info_label.setText("MP4を書き出し中... しばらくお待ち下さい")
        self.progress_bar.setValue(0)
        QApplication.processEvents()
        
        preview_mp4 = os.path.join(self.cache_dir, "preview.mp4")
        inputs =["-i", preview_mp4]
        filter_parts = []
        mix_labels =[]
        a_idx = 1
        
        if self.audio_path:
            inputs.extend(["-i", self.audio_path])
            filter_parts.append(f"[{a_idx}:a]adelay=0|0[a{a_idx}];")
            mix_labels.append(f"[a{a_idx}]")
            a_idx += 1
            
        for p_idx, part in enumerate(self.parts):
            if part.audio_file:
                inputs.extend(["-i", part.audio_file])
                for st_ms in self.part_start_times[p_idx]:
                    delay = int(st_ms)
                    filter_parts.append(f"[{a_idx}:a]adelay={delay}|{delay}[a{a_idx}];")
                    mix_labels.append(f"[a{a_idx}]")
                a_idx += 1

        if mix_labels:
            mc = len(mix_labels)
            filter_str = "".join(filter_parts) + "".join(mix_labels) + f"amix=inputs={mc}:normalize=0[aout]"
            cmd = ["ffmpeg", "-y"] + inputs +["-filter_complex", filter_str, "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", save_path]
        else:
            cmd =["ffmpeg", "-y", "-i", preview_mp4, "-c:v", "copy", save_path]
            
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            subprocess.run(cmd, check=True, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            QMessageBox.information(self, "完了", "音声付きMP4の書き出しが完了しました！")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"書き出しエラー:\n{e}")
        finally:
            self.info_label.setText("待機中")
            self.progress_bar.setValue(100)

    def closeEvent(self, e):
        if self.executor: self.executor.shutdown(wait=False, cancel_futures=True)
        if self.temp_dir: shutil.rmtree(self.temp_dir, ignore_errors=True)
        if self.cache_dir: shutil.rmtree(self.cache_dir, ignore_errors=True)
        e.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BootAnimationStudio()
    # イベントフィルターをアプリ全体にインストール (スペースキー対策)
    app.installEventFilter(KeyPressFilter(window))
    window.show()
    sys.exit(app.exec())