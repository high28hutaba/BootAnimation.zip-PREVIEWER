import sys, os, zipfile, tempfile, shutil, re, io, threading, queue, base64
from dataclasses import dataclass
from typing import List, Tuple
from PIL import Image, ImageDraw
from concurrent.futures import ProcessPoolExecutor, as_completed

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSlider, QListWidget, QMessageBox, QComboBox, QSpinBox, QLineEdit, 
    QGroupBox, QFormLayout, QProgressBar
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap

# ---------- ユーティリティ ----------

def get_checkerboard_css() -> str:
    """ グレーと白のチェック模様背景のCSSを生成 (エラー修正版) """
    try:
        img = Image.new("RGB", (20, 20), (200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 9, 9], fill=(255, 255, 255))
        draw.rectangle([10, 10, 19, 19], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        # 改行を除去し、明示的にスラッシュを使用する
        b64 = base64.b64encode(buf.getvalue()).decode('ascii').replace('\n', '')
        return f"background-image: url('data:image/png;base64,{b64}'); background-repeat: repeat;"
    except Exception as e:
        print(f"CSS生成エラー: {e}")
        return "background-color: #cccccc;"

def parse_trim_file(path: str) -> List[Tuple[int, int]]:
    """ trim.txt を解析し (x, y) オフセットのリストを返す """
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
    """ カラーコードを RGBAタプルに変換 """
    s = hexstr.strip().replace("#", "")
    try:
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
        elif len(s) == 8:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    except:
        pass
    return (0, 0, 0, 255)

# ---------- レンダリング・ワーカー ----------

def render_frame_worker(args):
    """ 画像合成処理（重い処理なので別プロセスで実行） """
    try:
        flat_idx, img_path, offset, desc_w, desc_h, bgcolor, dev_w, dev_h = args
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
        
        bio = io.BytesIO()
        final.save(bio, format="PNG")
        return (flat_idx, bio.getvalue(), None)
    except Exception as e:
        return (flat_idx, None, str(e))

@dataclass
class PartDef:
    mode: str
    count: int
    delay: int
    folder: str
    bgcolor: str
    offsets: List[Tuple[int, int]]
    has_trim: bool

class BootAnimationStudio(QWidget):
    def __init__(self):
        super().__init__()
        # タイトル変更
        self.setWindowTitle("BootAnim Previewer - Created By.High28(ふたば)")
        self.resize(1150, 750)
        
        self.temp_dir = None
        self.parts: List[PartDef] = []
        self.frame_paths: List[List[str]] = []
        self.desc_w = 0; self.desc_h = 0; self.base_fps = 30
        
        self.playing = False
        self.cur_p = 0      # 現在のパートインデックス
        self.cur_f = 0      # 現在のフレームインデックス
        self.part_loop_counter = 0 # 現在のパートを何回ループしたか
        
        self.render_cache = {}
        self.executor = None
        self.msg_queue = queue.Queue()
        self.internal_selection = False # リスト選択のプログラム制御フラグ

        self.init_ui()
        
        self.timer = QTimer(); self.timer.timeout.connect(self.next_frame)
        self.ui_update_timer = QTimer(); self.ui_update_timer.timeout.connect(self.process_queue); self.ui_update_timer.start(30)

    def init_ui(self):
        # メインビュー
        self.view = QLabel("ここに bootanimation.zip をドロップ")
        self.view.setAlignment(Qt.AlignCenter)
        cb_style = get_checkerboard_css()
        self.view.setStyleSheet(f"{cb_style} border: 2px solid #555; border-radius: 5px;")
        self.view.setMinimumSize(640, 480)

        # コントロール
        self.btn_play = QPushButton("再生"); self.btn_play.clicked.connect(self.play)
        self.btn_pause = QPushButton("一時停止"); self.btn_pause.clicked.connect(self.pause)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(self.manual_seek)
        
        self.info_label = QLabel("待機中")
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
        self.spin_fps.valueChanged.connect(self.on_fps_changed)
        
        self.btn_apply = QPushButton("適用 & キャッシュ再生成")
        self.btn_apply.clicked.connect(self.start_render)

        form.addRow("プリセット:", self.combo_preset)
        size_layout = QHBoxLayout(); size_layout.addWidget(self.edit_w); size_layout.addWidget(QLabel("x")); size_layout.addWidget(self.edit_h)
        form.addRow("画面サイズ:", size_layout)
        form.addRow("FPS (速度):", self.spin_fps)
        form.addRow(self.btn_apply)
        group.setLayout(form)
        
        self.list_parts = QListWidget()
        self.list_parts.currentRowChanged.connect(self.on_part_selected)

        self.side.addWidget(group)
        self.side.addWidget(QLabel("パーツリスト (クリックでジャンプ):"))
        self.side.addWidget(self.list_parts)

        layout = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(self.view, 1)
        btns = QHBoxLayout(); btns.addWidget(self.btn_play); btns.addWidget(self.btn_pause)
        left.addLayout(btns)
        left.addWidget(self.slider)
        left.addWidget(self.info_label)
        left.addWidget(self.progress_bar)
        
        layout.addLayout(left, 3)
        layout.addLayout(self.side, 1)
        self.setLayout(layout)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e): 
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def dropEvent(self, e): 
        self.load_zip(e.mimeData().urls()[0].toLocalFile())

    def load_zip(self, zip_path):
        if self.temp_dir: shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = tempfile.mkdtemp()
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(self.temp_dir)
            
            desc_path = None
            for root, _, files in os.walk(self.temp_dir):
                if "desc.txt" in files: desc_path = os.path.join(root, "desc.txt"); break

            if not desc_path: raise Exception("desc.txt が見つかりませんでした")

            with open(desc_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]

            # ヘッダー情報の読み込み
            header = lines[0].split()
            self.desc_w, self.desc_h, self.base_fps = int(header[0]), int(header[1]), int(header[2])
            
            # UI初期化
            self.edit_w.setText(str(self.desc_w)); self.edit_h.setText(str(self.desc_h))
            self.spin_fps.blockSignals(True)
            self.spin_fps.setValue(self.base_fps)
            self.spin_fps.blockSignals(False)
            self.combo_preset.setCurrentText("オリジナルサイズ (desc.txt)")

            self.parts, self.frame_paths = [], []
            self.list_parts.clear()
            
            base_dir = os.path.dirname(desc_path)
            for i, line in enumerate(lines[1:]):
                p_items = line.split()
                if len(p_items) < 4: continue
                
                # desc.txt 形式: type count delay path [bg]
                mode, count, delay, folder = p_items[0], int(p_items[1]), int(p_items[2]), p_items[3]
                bgcolor = p_items[4] if len(p_items) > 4 else "000000"
                
                f_dir = os.path.join(base_dir, folder)
                if not os.path.exists(f_dir):
                    # パスが見つからない場合はスキップ
                    continue

                frames = sorted([os.path.join(f_dir, f) for f in os.listdir(f_dir) if f.lower().endswith(('.png', '.jpg'))])
                
                trim_path = os.path.join(f_dir, "trim.txt")
                trim_offsets = parse_trim_file(trim_path)
                has_trim = len(trim_offsets) > 0
                
                self.parts.append(PartDef(mode, count, delay, folder, bgcolor, trim_offsets, has_trim))
                self.frame_paths.append(frames)
                
                # リスト表示用テキスト
                trim_str = "Trimあり" if has_trim else "Trimなし"
                loop_str = "無限ループ" if count == 0 else f"{count}回ループ"
                self.list_parts.addItem(f"Part {i}: {folder} ({len(frames)}f) [{loop_str}] [{trim_str}]")

            self.total_frames = sum(len(f) for f in self.frame_paths)
            self.slider.setRange(0, self.total_frames - 1)
            self.cur_p = 0; self.cur_f = 0; self.part_loop_counter = 0
            
            self.start_render()
            
            # 最初のパートを選択状態に
            if self.parts:
                self.highlight_part(0)

        except Exception as e:
            QMessageBox.critical(self, "読み込みエラー", str(e))

    def start_render(self):
        if not self.parts: return
        self.pause()
        self.render_cache.clear()
        self.progress_bar.setValue(0)
        
        target_w = int(self.edit_w.text()) if self.edit_w.text().isdigit() else self.desc_w
        target_h = int(self.edit_h.text()) if self.edit_h.text().isdigit() else self.desc_h
        
        if self.executor: self.executor.shutdown(wait=False, cancel_futures=True)
        self.executor = ProcessPoolExecutor(max_workers=os.cpu_count())
        
        tasks = []
        flat_idx = 0
        for p_idx, frames in enumerate(self.frame_paths):
            part = self.parts[p_idx]
            for f_idx, f_path in enumerate(frames):
                offset = part.offsets[f_idx] if f_idx < len(part.offsets) else None
                tasks.append((flat_idx, f_path, offset, self.desc_w, self.desc_h, part.bgcolor, target_w, target_h))
                flat_idx += 1
        
        self.total_to_render = len(tasks)
        if self.total_to_render == 0: return

        def run():
            for fut in as_completed([self.executor.submit(render_frame_worker, t) for t in tasks]):
                self.msg_queue.put(fut.result())
        threading.Thread(target=run, daemon=True).start()

    def process_queue(self):
        while not self.msg_queue.empty():
            idx, data, err = self.msg_queue.get()
            if data:
                self.render_cache[idx] = data
                val = int(len(self.render_cache) / self.total_to_render * 100)
                self.progress_bar.setValue(val)
        if not self.playing: self.update_display()

    def update_display(self):
        if not self.parts: return
        
        # フラットインデックス（全体を通したフレーム番号）を計算（スライダー表示用）
        current_flat_idx = 0
        for i in range(self.cur_p):
            current_flat_idx += len(self.frame_paths[i])
        current_flat_idx += self.cur_f
        
        data = self.render_cache.get(current_flat_idx)
        
        if data:
            pix = QPixmap()
            pix.loadFromData(data)
            self.view.setPixmap(pix.scaled(self.view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            # キャッシュ待ち画面
            self.view.clear()
            self.view.setText(
                "<div align='center' style='background-color: rgba(50, 50, 50, 0.85); color: white; "
                "padding: 20px; border-radius: 10px; font-size: 16px;'>"
                "<b>⏳ レンダリング中...</b><br><br>"
                f"Part: {self.cur_p} | Frame: {self.cur_f}</div>"
            )
        
        # 情報ラベル更新
        part = self.parts[self.cur_p]
        loop_info = "無限" if part.count == 0 else f"{self.part_loop_counter}/{part.count}"
        self.info_label.setText(
            f"Part: {self.cur_p} ({part.folder}) | Frame: {self.cur_f+1}/{len(self.frame_paths[self.cur_p])} | "
            f"Loop: {loop_info} | Total: {current_flat_idx}"
        )
        
        # スライダー同期
        self.slider.blockSignals(True)
        self.slider.setValue(current_flat_idx)
        self.slider.blockSignals(False)
        
        # リストのハイライト更新
        self.highlight_part(self.cur_p)

    def next_frame(self):
        if not self.parts: return
        
        current_part_frames = self.frame_paths[self.cur_p]
        part_def = self.parts[self.cur_p]
        
        self.cur_f += 1
        
        # 現在のパートの最終フレームを超えた場合
        if self.cur_f >= len(current_part_frames):
            self.part_loop_counter += 1
            
            # ループ判定 logic
            # count=0 は無限ループ。count>0 はその回数再生したら次へ。
            should_loop = False
            if part_def.count == 0:
                should_loop = True # 無限
            elif self.part_loop_counter < part_def.count:
                should_loop = True # 指定回数に満たない
            
            if should_loop:
                self.cur_f = 0
            else:
                # 次のパートへ
                next_p = self.cur_p + 1
                if next_p < len(self.parts):
                    self.cur_p = next_p
                    self.cur_f = 0
                    self.part_loop_counter = 0
                else:
                    # 全パート終了時の挙動（通常は最後のパートが無限ループ設定になっているはずだが、なっていなければ停止）
                    # ここでは最初のパートに戻らず、最後のフレームで停止させるか、再生を止める
                    self.pause()
                    self.cur_f = len(current_part_frames) - 1 # 最後のフレームに戻す
                    
        self.update_display()

    def play(self): 
        if not self.parts: return
        self.playing = True
        fps = self.spin_fps.value()
        interval = 1000 // fps if fps > 0 else 33
        self.timer.start(interval)

    def pause(self): 
        self.playing = False
        self.timer.stop()

    def on_fps_changed(self, val):
        """ FPS変更時にタイマー間隔を即時反映 """
        if self.playing:
            self.timer.stop()
            self.play()

    def manual_seek(self, val):
        """ スライダー操作時のシーク """
        accum = 0
        found = False
        for i, frames in enumerate(self.frame_paths):
            if val < accum + len(frames):
                self.cur_p = i
                self.cur_f = val - accum
                self.part_loop_counter = 0 # シークしたらループカウントはリセット
                found = True
                break
            accum += len(frames)
        if found:
            self.update_display()

    def highlight_part(self, index):
        """ プログラム側からリストの選択状態を変更（シグナル発火防止） """
        if 0 <= index < self.list_parts.count():
            self.internal_selection = True
            self.list_parts.setCurrentRow(index)
            self.internal_selection = False

    def on_part_selected(self, idx):
        """ リストクリック時の動作 """
        if self.internal_selection: return # プログラムによる変更なら無視
        
        if idx >= 0 and idx < len(self.parts):
            # クリックされたらそのパートの先頭へジャンプ
            self.cur_p = idx
            self.cur_f = 0
            self.part_loop_counter = 0
            self.update_display()
            # 再生中ならそのまま再生継続

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.playing and self.parts:
            self.update_display()

    def closeEvent(self, e):
        if self.executor: self.executor.shutdown(wait=False)
        if self.temp_dir: shutil.rmtree(self.temp_dir, ignore_errors=True)
        e.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BootAnimationStudio()
    window.show()
    sys.exit(app.exec())