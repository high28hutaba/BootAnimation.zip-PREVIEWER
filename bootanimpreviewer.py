import sys, os, zipfile, tempfile, shutil, re, io, threading, queue, subprocess, time
from dataclasses import dataclass
from typing import List, Tuple
from concurrent.futures import ProcessPoolExecutor, as_completed

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSlider, QListWidget, QMessageBox, QComboBox, QSpinBox, QLineEdit, 
    QGroupBox, QFormLayout, QProgressBar, QFileDialog, QStackedWidget
)
from PySide6.QtCore import Qt, QTimer, QUrl, QObject, QEvent
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PIL import Image

# ---------- イベントフィルター ----------

class KeyPressFilter(QObject):
    def __init__(self, main_win):
        super().__init__(main_win)
        self.main_win = main_win

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space:
            if isinstance(QApplication.focusWidget(), QLineEdit) or isinstance(QApplication.focusWidget(), QSpinBox):
                return False
            self.main_win.toggle_play()
            return True
        return super().eventFilter(obj, event)

# ---------- ユーティリティ ----------

def parse_trim_file(path: str) -> List[Tuple[int, int]]:
    offsets = []
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

        if offset:
            canvas = Image.new("RGBA", (desc_w, desc_h), bg_col)
            canvas.paste(img, offset, img)
        else:
            iw, ih = img.width, img.height
            scale_img = min(desc_w / iw, desc_h / ih)
            nw, nh = max(1, int(iw * scale_img)), max(1, int(ih * scale_img))
            img_scaled = img.resize((nw, nh), resample=Image.LANCZOS)
            canvas = Image.new("RGBA", (desc_w, desc_h), bg_col)
            canvas.paste(img_scaled, ((desc_w - nw)//2, (desc_h - nh)//2), img_scaled)

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

def _ensure_png_or_blank(src_path, width, height, out_path):
    try:
        with open(src_path, 'rb') as f:
            head = f.read(8)
            if head.startswith(b'\x89PNG\r\n\x1a\n'):
                try:
                    os.link(src_path, out_path)
                    return True
                except Exception:
                    try:
                        shutil.copyfile(src_path, out_path)
                        return True
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        blank = Image.new('RGBA', (width, height), (0,0,0,255))
        tmp = out_path + '.tmp'
        blank.save(tmp, format='PNG')
        os.replace(tmp, out_path)
    except Exception:
        try:
            open(out_path, 'wb').close()
        except Exception:
            pass
    return False

@dataclass
class PartDef:
    mode: str
    count: int
    delay: int
    folder: str
    bgcolor: str
    offsets: List[Tuple[int, int]]
    has_trim: bool
    audio_file: str

class BootAnimationStudio(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BootAnim Previewer - Created By.High28(ふたば)[エラー対策版]")
        self.resize(1150, 750)
        
        self.render_id = 0
        self.temp_dir = None
        self.cache_dir = None
        self.parts: List[PartDef] = []
        self.frame_paths: List[List[str]] = []
        self.part_start_flat_indices = []
        
        self.desc_w = 0; self.desc_h = 0; self.base_fps = 30
        self.playing = False
        
        self.render_cache = {}
        self.executor = None
        self.msg_queue = queue.Queue()
        self.render_start_time = 0
        self.audio_path = None
        
        self.preview_mp4_ready = False
        self.timeline = []
        self.part_start_times = {}
        self.current_playing_part = -1
        self.last_played_audio_marker = None
        
        self.init_ui()
        
        self.setAcceptDrops(True)
        
        self.ui_update_timer = QTimer()
        self.ui_update_timer.timeout.connect(self.process_queue)
        self.ui_update_timer.start(50)

    def init_ui(self):
        self.stacked_view = QStackedWidget()
        self.stacked_view.setMinimumSize(640, 480)

        self.drop_target_label = QLabel("ここに ZIP ファイルを\nドラッグ＆ドロップしてください")
        self.drop_target_label.setAlignment(Qt.AlignCenter)
        self.drop_target_label.setStyleSheet("background-color: #1e1e1e; color: #aaa; font-size: 24px; border: 4px dashed #555; border-radius: 10px;")

        # ★ 背景色を黒(#000)からグレー(#2a2a2a)に変更
        self.view = QVideoWidget()
        self.view.setStyleSheet("background-color: #2a2a2a; border: 2px solid #555; border-radius: 5px;")

        self.stacked_view.addWidget(self.drop_target_label)
        self.stacked_view.addWidget(self.view)

        self.media_player = QMediaPlayer()
        self.media_player.setVideoOutput(self.view)
        self.media_player.positionChanged.connect(lambda pos, s=self: getattr(s, 'on_position_changed', (lambda _: None))(pos))
        self.media_player.mediaStatusChanged.connect(lambda status, s=self: getattr(s, 'on_media_status_changed', (lambda _: None))(status))

        self.bgm_audio_output = QAudioOutput()
        self.bgm_player = QMediaPlayer()
        self.bgm_player.setAudioOutput(self.bgm_audio_output)
        
        self.part_audio_output = QAudioOutput()
        self.part_audio_player = QMediaPlayer()
        self.part_audio_player.setAudioOutput(self.part_audio_output)

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
        
        self.info_label = QLabel("待機中: ZIPを画面内にドラッグ＆ドロップしてください")
        self.info_label.setStyleSheet("font-family: 'Consolas', monospace; font-size: 13px;")
        self.progress_bar = QProgressBar()

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
        
        left.addWidget(self.stacked_view, 1)
        
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

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith('.zip'):
                    event.accept()
                    self.drop_target_label.setText("クリックを離して読み込み開始\n" + os.path.basename(url.toLocalFile()))
                    self.drop_target_label.setStyleSheet("background-color: #2e8b57; color: white; font-size: 24px; border: 4px solid #fff; border-radius: 10px;")
                    return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_target_label.setText("ここに ZIP ファイルを\nドラッグ＆ドロップしてください")
        self.drop_target_label.setStyleSheet("background-color: #1e1e1e; color: #aaa; font-size: 24px; border: 4px dashed #555; border-radius: 10px;")

    def dropEvent(self, event):
        self.dragLeaveEvent(event)
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local = url.toLocalFile()
                if local.lower().endswith('.zip'):
                    event.accept()
                    self.info_label.setText("ZIPの読み込みを開始しました...")
                    self.load_zip(local)
                    return
        event.ignore()

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
            try:
                self.part_audio_player.stop()
            except Exception:
                pass
            if self.playing:
                self.bgm_player.setPosition(self.media_player.position())
                self.bgm_player.play()

    def load_zip(self, zip_path):
        if not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "エラー", "システムに ffmpeg が見つかりません。MP4化には ffmpeg が必須です。")
            return

        self.stacked_view.setCurrentIndex(0)

        try:
            self.part_audio_player.stop()
            self.part_audio_player.setSource(QUrl())
        except Exception:
            pass
        try:
            self.bgm_player.stop()
            self.bgm_player.setSource(QUrl())
            self.audio_path = None
            self.btn_load_audio.setText("全体BGMを追加 (任意)")
        except Exception:
            pass
        self.last_played_audio_marker = None

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
                lines = [l.strip() for l in f.readlines() if l.strip()]

            header = lines[0].split()
            self.desc_w, self.desc_h, self.base_fps = int(header[0]), int(header[1]), int(header[2])
            
            self.edit_w.setText(str(self.desc_w)); self.edit_h.setText(str(self.desc_h))
            self.spin_fps.blockSignals(True)
            self.spin_fps.setValue(self.base_fps)
            self.spin_fps.blockSignals(False)

            new_parts, new_frame_paths, new_part_start_flat_indices = [], [], []
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
                
                part_audio = None
                for ext in ['wav', 'mp3', 'ogg']:
                    aud_path = os.path.join(f_dir, f"audio.{ext}")
                    if os.path.exists(aud_path):
                        part_audio = aud_path
                        break
                
                new_part_start_flat_indices.append(flat_idx_counter)
                flat_idx_counter += len(frames)
                
                new_parts.append(PartDef(mode, count, delay, folder, bgcolor, trim_offsets, len(trim_offsets)>0, part_audio))
                new_frame_paths.append(frames)
                
                aud_str = "🎵あり" if part_audio else "無音"
                loop_str = "無限" if count == 0 else f"{count}回"
                self.list_parts.addItem(f"Part {i}: {folder} ({len(frames)}f)[ループ:{loop_str}][{aud_str}]")

            self.parts = new_parts
            self.frame_paths = new_frame_paths
            self.part_start_flat_indices = new_part_start_flat_indices

            self.start_render()

        except Exception as e:
            QMessageBox.critical(self, "読み込みエラー", str(e))

    def start_render(self):
        if not self.parts: return
        self.pause()
        
        try:
            self.media_player.stop()
            self.media_player.setSource(QUrl())
        except Exception:
            pass

        self.preview_mp4_ready = False
        self.render_cache.clear()
        self.progress_bar.setValue(0)
        
        self.render_id += 1
        current_render_id = self.render_id
        
        target_w = int(self.edit_w.text()) if self.edit_w.text().isdigit() else self.desc_w
        target_h = int(self.edit_h.text()) if self.edit_h.text().isdigit() else self.desc_h
        
        if target_w % 2 != 0: target_w += 1
        if target_h % 2 != 0: target_h += 1
        self.edit_w.setText(str(target_w))
        self.edit_h.setText(str(target_h))
        
        if self.executor: self.executor.shutdown(wait=False, cancel_futures=True)
        self.executor = ProcessPoolExecutor(max_workers=max(1, os.cpu_count() - 1))
        
        tasks = []
        for p_idx, frames in enumerate(self.frame_paths):
            start_flat = self.part_start_flat_indices[p_idx]
            part = self.parts[p_idx]
            for f_idx, f_path in enumerate(frames):
                offset = part.offsets[f_idx] if f_idx < len(part.offsets) else None
                tasks.append((start_flat + f_idx, f_path, offset, self.desc_w, self.desc_h, part.bgcolor, target_w, target_h, self.cache_dir))
        
        self.total_to_render = len(tasks)
        if self.total_to_render == 0: return

        self.render_start_time = time.time()

        def run_all():
            futures = [self.executor.submit(render_frame_worker, t) for t in tasks]
            for fut in as_completed(futures):
                if self.render_id != current_render_id:
                    return
                try:
                    self.msg_queue.put(fut.result())
                except Exception:
                    pass

            if self.render_id != current_render_id:
                return

            self.msg_queue.put(("status", "画像合成完了。動画作成中... (ffmpeg)"))

            mp4_path = self.generate_preview_mp4_sync(current_render_id)
            if mp4_path and self.render_id == current_render_id:
                self.msg_queue.put(("video", mp4_path))

        threading.Thread(target=run_all, daemon=True).start()

    def generate_preview_mp4_sync(self, current_render_id):
        fps = self.spin_fps.value()
        seq_dir = os.path.join(self.cache_dir, "sequence")
        if os.path.exists(seq_dir):
            for f in os.listdir(seq_dir):
                try:
                    os.remove(os.path.join(seq_dir, f))
                except Exception:
                    pass
        os.makedirs(seq_dir, exist_ok=True)

        seq_index = 0
        new_timeline = []
        new_part_start_times = {}

        for p_idx, part in enumerate(self.parts):
            if self.render_id != current_render_id: return None
            if p_idx >= len(self.frame_paths): continue
            
            frames = self.frame_paths[p_idx]
            loop_count = part.count if part.count > 0 else 3
            new_part_start_times[p_idx] = []
            
            for loop_i in range(loop_count):
                if self.render_id != current_render_id: return None
                new_part_start_times[p_idx].append(seq_index * (1000.0 / fps))
                for f_idx in range(len(frames)):
                    flat_idx = self.part_start_flat_indices[p_idx] + f_idx
                    src = os.path.join(self.cache_dir, f"frame_{flat_idx:06d}.png")
                    if not os.path.exists(src):
                        blank = Image.new('RGBA', (self.desc_w, self.desc_h), (0,0,0,255))
                        blank_path = os.path.join(self.cache_dir, f"frame_{flat_idx:06d}.png")
                        blank.save(blank_path)
                        src = blank_path

                    seq_index += 1
                    dst = os.path.join(seq_dir, f"seq_{seq_index:06d}.png")
                    try:
                        _ensure_png_or_blank(src, self.desc_w, self.desc_h, dst)
                    except Exception:
                        try:
                            shutil.copyfile(src, dst)
                        except Exception:
                            blank = Image.new('RGBA', (self.desc_w, self.desc_h), (0,0,0,255))
                            blank.save(dst)

                    new_timeline.append((p_idx, f_idx, loop_i))

                if part.delay > 0:
                    last_flat = self.part_start_flat_indices[p_idx] + len(frames) - 1
                    last_src = os.path.join(self.cache_dir, f"frame_{last_flat:06d}.png")
                    for _ in range(part.delay):
                        seq_index += 1
                        dst = os.path.join(seq_dir, f"seq_{seq_index:06d}.png")
                        try:
                            _ensure_png_or_blank(last_src, self.desc_w, self.desc_h, dst)
                        except Exception:
                            try:
                                shutil.copyfile(last_src, dst)
                            except Exception:
                                blank = Image.new('RGBA', (self.desc_w, self.desc_h), (0,0,0,255))
                                blank.save(dst)
                        new_timeline.append((p_idx, len(frames)-1, loop_i))

        if self.render_id != current_render_id: return None
        
        self.timeline = new_timeline
        self.part_start_times = new_part_start_times

        mp4_path = os.path.join(self.cache_dir, f"preview_{current_render_id}.mp4")
        pattern = os.path.join(seq_dir, "seq_%06d.png")
        cmd = [
            "ffmpeg", "-y", "-f", "image2", "-framerate", str(fps), "-i", pattern,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", mp4_path
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            proc = subprocess.run(cmd, creationflags=flags, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                err = proc.stderr.decode('utf-8', errors='ignore')
                first_lines = '\n'.join(err.splitlines()[:12])
                try:
                    with open(os.path.join(self.cache_dir, 'ffmpeg_error_log.txt'), 'wb') as fh:
                        fh.write(proc.stderr)
                except Exception:
                    pass
                self.msg_queue.put(("status", f"ffmpeg エラー: {first_lines}"))
        except Exception as e:
            self.msg_queue.put(("status", f"ffmpeg 実行エラー: {e}"))

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
            elif msg[0] == "error":
                self.info_label.setText(f"エラー: {msg[3]}")

        if updated and not self.preview_mp4_ready:
            processed = len(self.render_cache)
            val = int(processed / self.total_to_render * 95)
            if val > 95: val = 95
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
        self.slider.setRange(0, len(self.timeline) - 1 if self.timeline else 0)
        
        self.stacked_view.setCurrentIndex(1)
        
        try:
            self.media_player.stop()
            self.media_player.setSource(QUrl.fromLocalFile(mp4_path))
        except Exception:
            self.media_player.setSource(QUrl.fromLocalFile(mp4_path))
        self.play()

    def on_position_changed(self, pos_ms):
        if not self.timeline: return
        fps = self.spin_fps.value()
        idx = int(pos_ms / 1000.0 * fps)
        if idx >= len(self.timeline): idx = len(self.timeline) - 1
        
        p_idx, f_idx, loop_idx = self.timeline[idx]
        
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)
        
        total_f = len(self.frame_paths[p_idx])
        self.part_progress_slider.blockSignals(True)
        self.part_progress_slider.setMaximum(total_f - 1)
        self.part_progress_slider.setValue(f_idx)
        self.part_progress_slider.blockSignals(False)
        self.part_progress_label.setText(f"Part {p_idx} 進捗: {f_idx + 1} / {total_f}")

        current_marker = (p_idx, loop_idx)
        if self.last_played_audio_marker != current_marker:
            self.last_played_audio_marker = current_marker
            self.list_parts.clearSelection()
            self.list_parts.setCurrentRow(p_idx)
            
            aud = self.parts[p_idx].audio_file
            try:
                self.part_audio_player.stop()
                self.part_audio_player.setSource(QUrl())
            except Exception:
                pass
            
            if aud and not self.audio_path:
                self.part_audio_player.setSource(QUrl.fromLocalFile(aud))
                self.part_audio_player.setPosition(0)
                if self.playing: self.part_audio_player.play()

    def on_media_status_changed(self, status):
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
        try:
            if not self.audio_path:
                self.part_audio_player.play()
        except Exception:
            pass
        if self.audio_path: self.bgm_player.play()

    def pause(self): 
        self.playing = False
        try:
            self.media_player.pause()
            self.part_audio_player.pause()
            self.bgm_player.pause()
        except Exception:
            pass

    def manual_seek(self, val):
        if not self.timeline: return
        fps = self.spin_fps.value()
        pos_ms = int(val * 1000.0 / fps)
        self.media_player.setPosition(pos_ms)
        
        if self.audio_path:
            self.bgm_player.setPosition(pos_ms)
        else:
            # ★ 個別音声をシークに同期させる処理を追加
            p_idx, f_idx, loop_idx = self.timeline[val]
            aud = self.parts[p_idx].audio_file
            if aud:
                # パートやループが変わった場合はソースを再セット
                if self.last_played_audio_marker != (p_idx, loop_idx):
                    self.part_audio_player.setSource(QUrl.fromLocalFile(aud))
                    self.last_played_audio_marker = (p_idx, loop_idx)
                
                # そのループの開始時間からのオフセットを計算してセット
                base_ms = self.part_start_times[p_idx][loop_idx]
                self.part_audio_player.setPosition(pos_ms - int(base_ms))
                if self.playing: self.part_audio_player.play()

    def manual_part_seek(self, val):
        if not self.timeline: return
        idx = self.list_parts.currentRow()
        if idx >= 0 and idx in self.part_start_times and self.part_start_times[idx]:
            loop_idx = self.last_played_audio_marker[1] if self.last_played_audio_marker else 0
            if loop_idx >= len(self.part_start_times[idx]): loop_idx = 0
            base_ms = self.part_start_times[idx][loop_idx]
            
            pos_ms = int(base_ms + (val * 1000.0 / self.spin_fps.value()))
            self.media_player.setPosition(pos_ms)

            # ★ パート内シーク時も音声を同期
            aud = self.parts[idx].audio_file
            if aud and not self.audio_path:
                audio_pos_ms = int(val * 1000.0 / self.spin_fps.value())
                self.part_audio_player.setPosition(audio_pos_ms)

    def on_part_clicked(self, item):
        idx = self.list_parts.row(item)
        if idx in self.part_start_times and self.part_start_times[idx]:
            self.last_played_audio_marker = None
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
        
        preview_mp4 = os.path.join(self.cache_dir, f"preview_{self.render_id}.mp4")
        inputs = ["-i", preview_mp4]
        filter_parts = []
        mix_labels = []
        a_idx = 1
        
        if self.audio_path:
            inputs.extend(["-i", self.audio_path])
            filter_parts.append(f"[{a_idx}:a]adelay=0|0[a{a_idx}];")
            mix_labels.append(f"[a{a_idx}]")
            a_idx += 1
        else:
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
            cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_str, "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", save_path]
        else:
            cmd = ["ffmpeg", "-y", "-i", preview_mp4, "-c:v", "copy", save_path]
            
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
    app.installEventFilter(KeyPressFilter(window))
    window.show()
    sys.exit(app.exec())
