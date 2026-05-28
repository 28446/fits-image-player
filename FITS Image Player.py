#!/usr/bin/env python3
"""
===============================================================================
🎯 FITS IMAGE PLAYER v1.0.0094 - SESSION PERSISTENCE & SIRILPY
===============================================================================
VERSION: 1.0.0093
FIXES:
- Siril Integration: Restored sirilpy module for auto-dependency management.
- Session Persistence: UI now loads the last used settings from the registry.
- Cache Logic: Automatically enables "Play Video" if existing cache matches saved settings.
- Grid Fix: Pixel-perfect grid rendering without aliasing.
"""

import os
import sys

# =============================================================================
# SIRIL DEPENDENCY MANAGEMENT (DO NOT REMOVE)
# =============================================================================
try:
    import sirilpy as s
except ImportError:
    print("Error: sirilpy module not found.")
    sys.exit(1)

s.ensure_installed("numpy", "opencv-python", "PyQt6", "astropy")

import re
import json
import time
import hashlib
import shutil
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from astropy.io import fits
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QSlider,
                             QPushButton, QGroupBox, QProgressBar, QComboBox,
                             QCheckBox, QDoubleSpinBox, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QScrollArea, QDialog, QDialogButtonBox,
                             QMessageBox, QLineEdit, QSpinBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRect, QPointF
from PyQt6.QtGui import QImage, QPainter, QPen, QColor, QPixmap, QFont

# =============================================================================
# SECTION 1: GLOBAL UTILITIES
# =============================================================================

VERSION = "1.0.0093"

def generate_params_hash(low, high, mid, bayer, rgb, scale, has_reg, hist_on, tele_on):
    """Generates a unique hash to validate cache consistency."""
    return hashlib.md5(f"{low}{high}{mid}{bayer}{rgb}{scale}{has_reg}{hist_on}{tele_on}".encode()).hexdigest()[:8]

def inspect_fits_metadata_safely(file_path):
    if not os.path.exists(file_path): return "MONO", "NONE"
    try:
        with fits.open(file_path, memmap=False) as hdul:
            hdu = next(h for h in hdul if h.data is not None)
            h = hdu.header
            pat = h.get('BAYERPAT', h.get('COLORTYP', '')).strip().upper()
            if not pat or pat == "NONE":
                return ("RGB", "RGGB") if hdu.data.ndim == 3 else ("MONO", "NONE")
            return "RGB", pat
    except: return "MONO", "NONE"

def execute_debayer_safely(mono, pat):
    try:
        p = str(pat).upper()
        if p == "NONE": return cv2.cvtColor(mono.astype(np.uint16), cv2.COLOR_GRAY2BGR)
        u16 = np.clip(mono, 0, 65535).astype(np.uint16)
        c_map = {"RGGB": cv2.COLOR_BayerBG2BGR, "BGGR": cv2.COLOR_BayerRG2BGR,
                 "GBRG": cv2.COLOR_BayerGR2BGR, "GRBG": cv2.COLOR_BayerGB2BGR}
        return cv2.cvtColor(u16, c_map.get(p, cv2.COLOR_BayerBG2BGR))
    except: return cv2.cvtColor(mono.astype(np.uint8), cv2.COLOR_GRAY2BGR)

def apply_pro_stretch(img_float, low_p, high_p, mid_p, is_rgb):
    if is_rgb and img_float.ndim == 3:
        for i in range(3):
            ch = img_float[:,:,i]
            med = np.median(ch)
            img_float[:,:,i] = np.clip(ch - med + 0.01, 0, None)
            c_max = np.max(img_float[:,:,i])
            if c_max > 0: img_float[:,:,i] /= c_max
    else:
        d_min, d_max = np.min(img_float), np.max(img_float)
        if d_max > d_min: img_float = (img_float - d_min) / (d_max - d_min)

    low, high = low_p / 100.0, high_p / 100.0
    res = np.clip((img_float - low) / (high - low if high > low else 1.0), 0, 1)
    m = max(0.0001, min(0.9999, 1.0 - (mid_p / 100.0)))
    res = (m - 1) * res / ((2 * m - 1) * res - m)
    return (np.clip(res, 0, 1) * 255).astype(np.uint8)

def draw_histogram_from_data(img8, width=180, height=45, save_path=None):
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.rectangle(canvas, (0,0), (width-1, height-1), (100,100,100), 1)
    hists = []
    if img8.ndim == 3:
        for i, c in enumerate([(255,0,0),(0,255,0),(0,0,255)]):
            hists.append((c, cv2.calcHist([img8],[i],None,[256],[0,256])))
    else:
        hists = [((255,255,255), cv2.calcHist([img8],[0],None,[256],[0,256]))]
    max_v = max(1.0, max(np.max(h[1]) for h in hists))
    pw, ph = width-2, height-2
    for color, arr in hists:
        for i in range(256):
            x = int((i/256.0)*pw); y = int((arr.item(i)/max_v)*ph)
            if y>0: cv2.line(canvas, (x+1, ph), (x+1, ph-y), color, 1)
    if save_path: cv2.imwrite(save_path, canvas)
    return canvas

def scan_for_lights_directories(root_path):
    dirs = []
    try:
        for item in os.listdir(root_path):
            fp = os.path.join(root_path, item)
            if os.path.isdir(fp):
                if item.lower() == "lights": dirs.append(os.path.abspath(fp))
                else:
                    for sub in os.listdir(fp):
                        if sub.lower() == "lights": dirs.append(os.path.join(os.path.abspath(fp), sub))
    except: pass
    return sorted(list(set(dirs)))

def gather_and_validate_fits_multi(directory_list):
    valid = []
    for d in directory_list:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(('.fits', '.fit')): valid.append(os.path.join(d, f))
    return sorted(valid)

def find_best_siril_seq(dir_path):
    try:
        process_path = os.path.join(os.path.dirname(dir_path), "process")
        if not os.path.exists(process_path): return None
        pattern = re.compile(r'^r_.*light.*\.seq$', re.IGNORECASE)
        seq_files = [f for f in os.listdir(process_path) if pattern.match(f)]
        return os.path.join(process_path, seq_files[0]) if seq_files else None
    except: return None

def parse_siril_seq_data(seq_path):
    data_dict = {}; h_info = {"base_name": "", "fixed_len": 5, "img_numbers": []}
    if not seq_path: return None, None
    try:
        with open(seq_path, 'r', errors='ignore') as f:
            lines = f.readlines()
            for line in lines:
                if line.startswith('S '):
                    parts = line.split("'")
                    if len(parts) >= 2:
                        h_info["base_name"] = parts[1]
                        meta = parts[2].strip().split()
                        if len(meta) >= 4: h_info["fixed_len"] = int(meta[3])
                if line.startswith('I '): h_info["img_numbers"].append(line.strip().split()[1])
            f_idx = 0
            for line in lines:
                if line.startswith('R0'):
                    parts = line.replace(",", ".").split()
                    entry = {'hfr': 0.0, 'selected': True, 'num': ""}
                    try:
                        fwhm = [float(x) for x in parts[1:7]]
                        if all(v == 0 for v in fwhm): entry['selected'] = False
                        entry['hfr'] = np.sqrt(max(0.0001, fwhm[0] * fwhm[1])) * 0.52
                    except: pass
                    if f_idx < len(h_info["img_numbers"]): entry['num'] = h_info["img_numbers"][f_idx]
                    data_dict[f_idx] = entry; f_idx += 1
    except: return None, None
    return data_dict, h_info

def format_coords(ra_val, dec_val):
    if ra_val is None or dec_val is None or str(ra_val) == "N/A": return "N/A"
    try:
        ra_deg, dec_deg = float(ra_val), float(dec_val)
        def to_hms(deg):
            h = int(deg/15); m = int((deg/15-h)*60); s = (deg/15-h-m/60)*3600
            return f"{h:02d}h{m:02d}m{s:04.1f}s"
        def to_dms(deg):
            sign = "+" if deg >= 0 else "-"; d = int(abs(deg)); m = int((abs(deg)-d)*60); s = (abs(deg)-d-m/60)*3600
            return f"{sign}{d:02d}d{m:02d}m{s:04.1f}s"
        return f"{to_hms(ra_deg)} / {to_dms(dec_deg)}"
    except: return f"{ra_val} / {dec_val}"

# =============================================================================
# SECTION 2: DIALOGS
# =============================================================================

class ManualStretchDialog(QDialog):
    def __init__(self, f_p, i_l, i_h, i_m, b, is_r, parent=None):
        super().__init__(parent); self.setWindowTitle("Manual Stretch Calibration"); self.setFixedSize(920, 820); self.low, self.high, self.mid, self.bayer, self.is_rgb = i_l, i_h, i_m, b, is_r
        with fits.open(f_p, memmap=False) as hd:
            hdu = next(h for h in hd if h.data is not None); raw = hdu.data.astype(np.float32); raw = np.nan_to_num(raw); self.d_min, self.d_max = np.min(raw), np.max(raw)
        sc = 880/raw.shape[1]; prx = cv2.resize(raw, (0,0), fx=sc, fy=sc)
        if self.is_rgb: prx = execute_debayer_safely(prx, self.bayer).astype(np.float32)
        pm, pM = np.min(prx), np.max(prx); self.prx_01 = (prx - pm) / (pM - pm if pM > pm else 1.0)
        self.setup_ui(); self.on_param_changed()

    def setup_ui(self):
        lay = QVBoxLayout(self); self.lp = QLabel(); self.lp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lp.setStyleSheet("background-color: #000; border: 1px solid #444;"); self.lp.setFixedSize(900, 500); lay.addWidget(self.lp)
        self.lh = QLabel(); self.lh.setFixedSize(900, 100); self.lh.setStyleSheet("background-color: #000; border: 1px solid #444;"); lay.addWidget(self.lh)
        grd = QGridLayout()
        self.sl, self.sh, self.sm = QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal)
        self.sl.setRange(0, 200); self.sl.setValue(int(self.low*10)); self.sh.setRange(800, 1000); self.sh.setValue(int(self.high*10)); self.sm.setRange(50, 100); self.sm.setValue(int(self.mid))
        for s in [self.sl, self.sh, self.sm]: s.valueChanged.connect(self.on_param_changed)
        self.ll, self.lh_v, self.lm = QLabel(""), QLabel(""), QLabel(""); grd.addWidget(self.ll, 0, 0); grd.addWidget(self.sl, 0, 1); grd.addWidget(self.lm, 1, 0); grd.addWidget(self.sm, 1, 1); grd.addWidget(self.lh_v, 2, 0); grd.addWidget(self.sh, 2, 1); lay.addLayout(grd)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel); bb.accepted.connect(self.accept); bb.rejected.connect(self.reject); lay.addWidget(bb)

    def on_param_changed(self):
        self.low, self.high, self.mid = self.sl.value()/10.0, self.sh.value()/10.0, self.sm.value()
        self.ll.setText(f"Black: {self.low}%"); self.lh_v.setText(f"White: {self.high}%"); self.lm.setText(f"Curve: {self.mid}"); self.update_preview()

    def update_preview(self):
        img8 = apply_pro_stretch(self.prx_01.copy(), self.low, self.high, self.mid, self.is_rgb)
        hi_img = draw_histogram_from_data(img8, 900, 100)
        self.lh.setPixmap(QPixmap.fromImage(QImage(cv2.cvtColor(hi_img, cv2.COLOR_BGR2RGB).data, 900, 100, 900*3, QImage.Format.Format_RGB888)))
        h, w = img8.shape[:2]; sc = min(900/w, 500/h); p_w, p_h = int(w*sc), int(h*sc)
        preview = cv2.resize(img8, (p_w, p_h), interpolation=cv2.INTER_AREA)
        if preview.ndim == 3: preview = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        q = QImage(preview.data, p_w, p_h, p_w*(3 if preview.ndim==3 else 1), QImage.Format.Format_RGB888 if preview.ndim==3 else QImage.Format.Format_Grayscale8)
        self.lp.setPixmap(QPixmap.fromImage(q))

    def get_values(self): return self.low, self.high, self.mid

# =============================================================================
# SECTION 3: PLAYER ENGINE
# =============================================================================

class AstroViewPlayerWindow(QMainWindow):
    def __init__(self, paths, metas, fps, rgb, save_video, poi_only, notes="", grid_en=False, grid_st=30.0):
        super().__init__(); self.paths, self.metas, self.fps, self.total = paths, metas, fps, len(paths); self.idx, self.paused = 0, False
        self.save_video, self.poi_only, self.notes, self.grid_en, self.grid_st = save_video, poi_only, notes, grid_en, grid_st
        self.is_drawing, self.ix, self.iy, self.cx, self.cy = False, -1, -1, -1, -1; self.crop_box, self.is_cropped, self.video_writer, self.recorded_count = None, False, None, 0
        img_check = cv2.imread(paths[0]) if paths else None
        self.h_img, self.w_img = img_check.shape[:2] if img_check is not None else (1080, 1920); self.aspect_ratio = self.w_img / self.h_img
        self.tele_active = metas[0].get("telemetry_active", True)
        self.band_h = 55 if self.tele_active else 0
        dt = QApplication.primaryScreen().availableGeometry(); tw, th = self.w_img, self.h_img + self.band_h
        if th > dt.height(): sc = (dt.height()-80)/th; tw, th = int(tw*sc), int(th*sc)
        self.resize(tw, th); self.vw = QWidget(); self.setCentralWidget(self.vw); self.vw.setMouseTracking(True)
        self.vw.paintEvent = self.on_paint; self.vw.mousePressEvent = self.on_press; self.vw.mouseMoveEvent = self.on_move; self.vw.mouseReleaseEvent = self.on_release; self.vw.mouseDoubleClickEvent = self.on_dbl_click
        self.timer = QTimer(); self.timer.timeout.connect(self.next_f); self.timer.start(int(fps*1000)); self.curr_q = None; self.prepare_f()

    def prepare_f(self):
        if self.idx >= len(self.paths): return
        path = self.paths[self.idx]; base = cv2.imread(path) if os.path.exists(path) else None
        if base is None: return
        m = self.metas[self.idx]
        if self.is_cropped and self.crop_box:
            active = cv2.resize(base[self.crop_box[1]:self.crop_box[3], self.crop_box[0]:self.crop_box[2]], (self.w_img, self.h_img))
        else: active = base.copy()

        if self.save_video and (not self.poi_only or self.is_cropped):
            f_f = np.zeros((self.h_img + self.band_h, self.w_img, 3), dtype=np.uint8); f_f[0:self.h_img, 0:self.w_img] = active
            if self.grid_en and self.grid_st > 5:
                g_col = (0, 0, 180)
                for x in range(0, self.w_img, int(self.grid_st)): cv2.line(f_f, (x, 0), (x, self.h_img), g_col, 1)
                for y in range(0, self.h_img, int(self.grid_st)): cv2.line(f_f, (0, y), (self.w_img, y), g_col, 1)
            if self.tele_active: self.draw_overlay_columns(f_f, m, is_cv2=True)
            if self.video_writer is None: self.video_writer = cv2.VideoWriter(f"capture_{int(time.time())}.webm", cv2.VideoWriter_fourcc(*'VP80'), 1.0/self.fps, (self.w_img, self.h_img + self.band_h))
            self.video_writer.write(f_f); self.recorded_count += 1
            if self.recorded_count >= self.total: self.save_video = False; self.release_writer()
        rgb = cv2.cvtColor(active, cv2.COLOR_BGR2RGB); self.curr_q = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.shape[1]*3, QImage.Format.Format_RGB888).copy()

    def draw_overlay_columns(self, target, m, is_cv2=False, p=None):
        y_b, sp = (self.h_img + self.band_h - 5, 15) if is_cv2 else (self.height()-5, 15)
        w = self.w_img if is_cv2 else self.width(); col_w = w // 5; hfr = m.get('hfr', 'N/A')
        if is_cv2:
            cv2.putText(target, f"Tgt: {m.get('target')}", (10, y_b-2*sp), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"Coord: {m.get('coords')}", (10, y_b-sp), 0, 0.38, (0,255,255), 1, 16)
            cv2.putText(target, f"Date: {m.get('date_obs')}", (10, y_b), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"Diameter: {m.get('diameter','N/A')}", (col_w, y_b-2*sp), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"F_len: {m.get('f_len','N/A')}", (col_w, y_b-sp), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"arcsecs/px: {m.get('pixel_scale','N/A')}", (col_w, y_b), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"Exp: {m.get('exp_time')}", (col_w*2, y_b-2*sp), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"Gain: {m.get('gain')}", (col_w*2, y_b-sp), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"HFR: {hfr}", (col_w*2, y_b), 0, 0.38, (0,255,0) if "px" in str(hfr) else (100,100,100), 1, 16)
            cv2.putText(target, f"File: {m.get('filename')}", (col_w*3, y_b-2*sp), 0, 0.38, (200,200,200), 1, 16)
            cv2.putText(target, f"Note:", (col_w*3, y_b-sp), 0, 0.38, (0,255,255), 1, 16)
            cv2.putText(target, f"{self.notes}", (col_w*3, y_b), 0, 0.38, (200,200,200), 1, 16)
        elif p:
            p.setPen(QColor(200,200,200)); p.drawText(10, y_b-2*sp, f"Tgt: {m.get('target')}")
            p.setPen(QColor(0,255,255)); p.drawText(10, y_b-sp, f"Coord: {m.get('coords')}")
            p.setPen(QColor(200,200,200)); p.drawText(10, y_b, f"Date: {m.get('date_obs')}")
            p.drawText(col_w, y_b-2*sp, f"Diameter: {m.get('diameter','N/A')}"); p.drawText(col_w, y_b-sp, f"F_len: {m.get('f_len','N/A')}"); p.drawText(col_w, y_b, f"arcsecs/px: {m.get('pixel_scale','N/A')}")
            p.drawText(col_w*2, y_b-2*sp, f"Exp: {m.get('exp_time')}"); p.drawText(col_w*2, y_b-sp, f"Gain: {m.get('gain')}")
            p.setPen(QColor(0,255,0) if "px" in str(hfr) else QColor(100,100,100)); p.drawText(col_w*2, y_b, f"HFR: {hfr}")
            p.setPen(QColor(200,200,200)); p.drawText(col_w*3, y_b-2*sp, f"File: {m.get('filename')}")
            p.setPen(QColor(0,255,255)); p.drawText(col_w*3, y_b-sp, "Note:"); p.setPen(QColor(200,200,200)); p.drawText(col_w*3, y_b, self.notes)

    def release_writer(self):
        if self.video_writer: self.video_writer.release(); self.video_writer = None
    def closeEvent(self, event):
        self.timer.stop(); self.release_writer(); event.accept()
    def next_f(self):
        if not self.paused: self.idx = (self.idx + 1) % self.total; self.prepare_f(); self.update()
    def on_paint(self, event):
        p = QPainter(self.vw); m = self.metas[self.idx]; p.fillRect(self.vw.rect(), QColor(0,0,0)); avail_h = self.height() - self.band_h
        img_aspect = self.w_img / self.h_img; win_aspect = self.width() / avail_h
        if win_aspect > img_aspect: draw_h = avail_h; draw_w = int(draw_h * img_aspect)
        else: draw_w = self.width(); draw_h = int(draw_w / img_aspect)
        self.img_rect = QRect((self.width()-draw_w)//2, (avail_h-draw_h)//2, draw_w, draw_h)
        if self.curr_q: p.drawImage(self.img_rect, self.curr_q)
        if self.grid_en and self.grid_st > 5:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False); p.setPen(QPen(QColor(255, 0, 0, 100), 1))
            num_v = self.img_rect.width() // int(self.grid_st)
            for i in range(num_v + 1):
                lx = self.img_rect.left() + i * int(self.grid_st)
                if lx <= self.img_rect.right(): p.drawLine(lx, self.img_rect.top(), lx, self.img_rect.bottom())
            num_h = self.img_rect.height() // int(self.grid_st)
            for j in range(num_h + 1):
                ly = self.img_rect.top() + j * int(self.grid_st)
                if ly <= self.img_rect.bottom(): p.drawLine(self.img_rect.left(), ly, self.img_rect.right(), ly)
        if self.tele_active:
            p.fillRect(0, self.height()-self.band_h, self.width(), self.band_h, QColor(0,0,0))
            p.setFont(QFont("Helvetica", 9)); self.draw_overlay_columns(None, m, is_cv2=False, p=p)
            if m.get("hist_enabled") and os.path.exists(m.get("hist_disk_path", "")):
                hist = cv2.imread(m["hist_disk_path"])
                if hist is not None:
                    hist_rgb = cv2.cvtColor(hist, cv2.COLOR_BGR2RGB); qh = QImage(hist_rgb.data, hist_rgb.shape[1], hist_rgb.shape[0], hist_rgb.shape[1]*3, QImage.Format.Format_RGB888)
                    p.drawImage(self.width() - qh.width() - 5, self.height() - qh.height() - 5, qh)
        if self.is_drawing: p.setPen(QPen(QColor(0,255,255), 1)); p.drawRect(QRect(int(self.ix), int(self.iy), int(self.cx-self.ix), int(self.cy-self.iy)))
        p.end()
    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space: self.paused = not self.paused
        elif e.key() == Qt.Key.Key_Left: self.paused = True; self.idx = (self.idx - 1 + self.total) % self.total; self.prepare_f(); self.update()
        elif e.key() == Qt.Key.Key_Right: self.paused = True; self.idx = (self.idx + 1) % self.total; self.prepare_f(); self.update()
        elif e.key() in [Qt.Key.Key_Escape, Qt.Key.Key_Q]: self.close()
    def on_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton: self.is_drawing = True; self.ix, self.iy = e.position().x(), e.position().y(); self.cx, self.cy = self.ix, self.iy
    def on_move(self, e):
        if self.is_drawing: self.cx = e.position().x(); dx = self.cx - self.ix; dy = dx / self.aspect_ratio; self.cy = self.iy + dy; self.update()
    def on_release(self, e):
        if self.is_drawing:
            self.is_drawing = False
            if self.img_rect.contains(int(self.ix), int(self.iy)):
                vw = (self.crop_box[2]-self.crop_box[0]) if self.is_cropped else self.w_img
                vh = (self.crop_box[3]-self.crop_box[1]) if self.is_cropped else self.h_img
                xr = int(((self.ix - self.img_rect.left()) / self.img_rect.width()) * vw) + (self.crop_box[0] if self.is_cropped else 0)
                yr = int(((self.iy - self.img_rect.top()) / self.img_rect.height()) * vh) + (self.crop_box[1] if self.is_cropped else 0)
                sel_w = int((abs(self.cx - self.ix) / self.img_rect.width()) * vw)
                if sel_w > 10:
                    self.crop_box = [xr, yr, xr + sel_w, yr + int(sel_w / self.aspect_ratio)]; self.is_cropped = True; self.prepare_f(); self.update()
    def on_dbl_click(self, e): self.is_cropped = False; self.crop_box = None; self.prepare_f(); self.update()

def run_opencv_player(p, m, f, rgb, s, poi, n, g, st):
    global player_win; player_win = AstroViewPlayerWindow(p, m, f, rgb, s, poi, n, g, st); player_win.show()

# =============================================================================
# SECTION 4: WORKER & MAIN GUI
# =============================================================================

def _process_single_frame_worker(args):
    f_p, low, high, mid, bayer, rgb, scale, c_d, p_h, s_item, h_info, use_siril, hist_on, tele_on = args
    if use_siril and s_item and not s_item.get('selected', True): return None
    src_p = f_p
    if use_siril and h_info and s_item and s_item.get('num'):
        proc_dir = os.path.join(os.path.dirname(os.path.dirname(f_p)), "process")
        cand_p = os.path.join(proc_dir, f"{h_info['base_name']}{str(s_item['num']).zfill(h_info['fixed_len'])}.fit")
        if os.path.exists(cand_p): src_p = cand_p

    fn = os.path.basename(f_p); out = os.path.join(c_d, f"{os.path.splitext(fn)[0]}_{p_h}.png"); hist_p = os.path.join(c_d, f"hist_{os.path.splitext(fn)[0]}_{p_h}.png")
    img_ready = os.path.exists(out); hist_needed = hist_on and not os.path.exists(hist_p)
    meta = {"filename": fn, "hist_disk_path": hist_p, "full_path": f_p, "hist_enabled": hist_on, "telemetry_active": tele_on}
    try:
        with fits.open(src_p, memmap=False) as hd:
            hdu = next(h for h in hd if h.data is not None); h, data = hdu.header, hdu.data
            ra, dec = h.get('OBJCTRA', h.get('RA', 'N/A')), h.get('OBJCTDEC', h.get('DEC', 'N/A'))
            meta.update({
                "date_obs": str(h.get('DATE-OBS', 'N/A')), "target": h.get('OBJECT', 'N/A'),
                "coords": format_coords(ra, dec), "gain": str(h.get('GAIN', 'N/A')),
                "exp_time": f"{h.get('EXPTIME', 0):.1f}s", "hfr": f"{s_item['hfr']:.2f}px" if s_item else "N/A",
                "diameter": str(h.get('APTDIA', 'N/A')), "f_len": str(h.get('FOCALLEN', 'N/A')),
                "pixel_scale": str(h.get('SCALE', h.get('PIXSCALE', 'N/A')))
            })
            if img_ready and not hist_needed: return fn, out, meta
            final_rgb, final_bayer = rgb, bayer
            if bayer == "AUTO":
                h_m, h_p = inspect_fits_metadata_safely(src_p)
                if h_m == "RGB": final_rgb, final_bayer = True, (h_p if h_p != "NONE" else "RGGB")
            img_processed = None
            if not img_ready:
                img_f = np.nan_to_num(data).astype(np.float32)
                if final_rgb: img_f = execute_debayer_safely(img_f, final_bayer).astype(np.float32)
                if scale != 1.0: img_f = cv2.resize(img_f, (0,0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                img_processed = apply_pro_stretch(img_f, low, high, mid, final_rgb); cv2.imwrite(out, img_processed)
            if hist_on:
                if img_processed is None: img_processed = cv2.imread(out)
                draw_histogram_from_data(img_processed, save_path=hist_p)
            return fn, out, meta
    except: return None

class PipelineWorker(QThread):
    progress_signal = pyqtSignal(int, int); finished_signal = pyqtSignal(list, list, float, bool)
    def __init__(self, f_f, l, h, m, b, is_r, sc, c_d, s_d, hi, use_s, hist_on, tele_on):
        super().__init__(); self.fits_files, self.low, self.high, self.mid, self.bayer, self.is_rgb, self.scale, self.cache_dir, self.s_data, self.h_info, self.use_siril, self.hist_on, self.tele_on = f_f, l, h, m, b, is_r, sc, c_d, s_d, hi, use_s, hist_on, tele_on; self.stop_requested = False
    def run(self):
        st = time.time(); p_h = generate_params_hash(self.low, self.high, self.mid, self.bayer, self.is_rgb, self.scale, self.use_siril, self.hist_on, self.tele_on)
        tasks = [(f, self.low, self.high, self.mid, self.bayer, self.is_rgb, self.scale, self.cache_dir, p_h, self.s_data.get(i) if self.s_data else None, self.h_info, self.use_siril, self.hist_on, self.tele_on) for i, f in enumerate(self.fits_files)]
        p_v, m_v = [], []
        with ThreadPoolExecutor() as ex:
            for i, res in enumerate(ex.map(_process_single_frame_worker, tasks)):
                if self.stop_requested: break
                if res: p_v.append(res[1]); m_v.append(res[2])
                self.progress_signal.emit(i+1, len(self.fits_files))
        self.finished_signal.emit(p_v, m_v, time.time()-st, self.is_rgb)

class FITSImagePlayerGui(QMainWindow):
    def __init__(self, d_d, c_d):
        super().__init__(); self.detected_dirs, self.cache_dir = d_d, c_d; self.is_processing = False
        self.current_low, self.current_high, self.current_mid = 0.2, 99.95, 95.0; self.last_duration = "N/A"
        self.setWindowTitle(f"FITS Image Player v{VERSION}"); self.setFixedSize(720, 680); self.setStyleSheet("QMainWindow, QWidget { background-color: #282828; color: #E0E0E0; font-family: Helvetica; } QGroupBox { border: 1px solid #646464; margin-top: 10px; padding-top: 10px; font-weight: bold; } QPushButton { background-color: #444; color: white; font-weight: bold; border-radius: 3px; }"); self.setup_ui()
        self.load_session_state(); self.update_stats()

    def setup_ui(self):
        cw = QWidget(); self.setCentralWidget(cw); layout = QVBoxLayout(cw); group_ds = QGroupBox(" Target Dataset Selection "); ds_lay = QVBoxLayout(group_ds); scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFixedHeight(160); scroll_w = QWidget(); scroll_lay = QVBoxLayout(scroll_w); self.chk_dirs = {}
        for d in self.detected_dirs:
            h = QHBoxLayout(); chk = QCheckBox(os.path.relpath(d, os.getcwd())); chk.stateChanged.connect(self.check_state); h.addWidget(chk); seq_p = find_best_siril_seq(d); found = bool(seq_p); lbl = QLabel(f"Siril data {'FOUND' if found else 'NOT FOUND'}"); lbl.setStyleSheet("color: #00FF00;" if found else "color: #777;"); lbl.setAlignment(Qt.AlignmentFlag.AlignRight); h.addWidget(lbl); scroll_lay.addLayout(h); self.chk_dirs[d] = chk
        scroll.setWidget(scroll_w); ds_lay.addWidget(scroll); layout.addWidget(group_ds)
        group_st = QGroupBox(" Output scale and Stretch "); st_lay = QVBoxLayout(group_st); row = QHBoxLayout()
        row.addWidget(QLabel("Scale (%):")); self.spin_scale = QSpinBox(); self.spin_scale.setRange(1, 100); self.spin_scale.setValue(50); self.spin_scale.setSuffix("%"); self.spin_scale.setFixedWidth(65); self.spin_scale.valueChanged.connect(self.check_state); row.addWidget(self.spin_scale)
        self.lbl_res_info = QLabel("Res: N/A"); self.lbl_res_info.setStyleSheet("color: #00FFFF; margin-left: 2px;"); row.addWidget(self.lbl_res_info)
        row.addSpacing(10); row.addWidget(QLabel("Grid:")); self.spin_grid = QDoubleSpinBox(); self.spin_grid.setRange(5, 1000); self.spin_grid.setValue(30.0); self.spin_grid.setSuffix("px"); self.spin_grid.setFixedWidth(75); row.addWidget(self.spin_grid)
        self.chk_grid = QCheckBox("Show"); row.addWidget(self.chk_grid); row.addStretch()
        self.btn_manual = QPushButton("Manual Stretch"); self.btn_manual.setFixedSize(110, 30); self.btn_manual.clicked.connect(self.open_manual_stretch); row.addWidget(self.btn_manual)
        self.chk_auto = QCheckBox("Auto"); self.chk_auto.setChecked(True); self.chk_auto.stateChanged.connect(self.toggle_stretch_mode); row.addWidget(self.chk_auto)
        st_lay.addLayout(row); layout.addWidget(group_st)
        group_ov = QGroupBox(" Overrides "); ov_lay = QHBoxLayout(group_ov); self.combo_render = QComboBox(); self.combo_render.addItems(["AUTO", "MONO", "RGB"]); self.combo_bayer = QComboBox(); self.combo_bayer.addItems(["AUTO", "RGGB", "BGGR", "GBRG", "GRBG"]); self.spin_fps = QDoubleSpinBox(); self.spin_fps.setRange(0.01, 5.0); self.spin_fps.setValue(0.10)
        ov_lay.addWidget(QLabel("Mode:")); ov_lay.addWidget(self.combo_render); ov_lay.addWidget(QLabel("Bayer:")); ov_lay.addWidget(self.combo_bayer); ov_lay.addWidget(QLabel("Delay (s):")); ov_lay.addWidget(self.spin_fps); layout.addWidget(group_ov)
        group_opt = QGroupBox(" Session Options "); opt_grid = QGridLayout(group_opt); self.txt_notes = QLineEdit(); self.txt_notes.setPlaceholderText("Filter, sky..."); opt_grid.addWidget(QLabel("Notes:"), 0, 0); opt_grid.addWidget(self.txt_notes, 0, 1, 1, 3); self.chk_save = QCheckBox("Save Video"); self.chk_poi = QCheckBox("Record ROI only"); self.chk_tele = QCheckBox("Telemetry"); self.chk_tele.setChecked(True); self.chk_hist = QCheckBox("Histogram"); self.chk_hist.setChecked(True); opt_grid.addWidget(self.chk_save, 1, 0); opt_grid.addWidget(self.chk_poi, 1, 1); opt_grid.addWidget(self.chk_tele, 2, 0); opt_grid.addWidget(self.chk_hist, 2, 1); layout.addWidget(group_opt)
        self.chk_tele.stateChanged.connect(self.toggle_tele_hist)
        self.lbl_status = QLabel("Status: Idle"); layout.addWidget(self.lbl_status); self.pbar = QProgressBar(); layout.addWidget(self.pbar); self.lbl_stats = QLabel(""); self.lbl_stats.setStyleSheet("color: #888; font-size: 11px;"); layout.addWidget(self.lbl_stats)
        btns = QHBoxLayout(); self.btn_act = QPushButton("Process Video"); self.btn_act.setFixedSize(180, 35); self.btn_act.clicked.connect(self.on_action); self.btn_clear = QPushButton("Clear Cache"); self.btn_clear.clicked.connect(self.clear_cache); btn_close = QPushButton("Close"); btn_close.setFixedSize(180, 35); btn_close.clicked.connect(self.close); btns.addWidget(self.btn_act); btns.addStretch(); btns.addWidget(self.btn_clear); btns.addWidget(btn_close); layout.addLayout(btns); self.toggle_stretch_mode(); self.toggle_tele_hist()

    def load_session_state(self):
        reg_p = os.path.join(self.cache_dir, "datasets_registry.json")
        if not os.path.exists(reg_p): return
        try:
            with open(reg_p, "r") as f:
                reg = json.load(f); s = reg.get("_last_ui_state")
                if s:
                    self.current_low, self.current_high, self.current_mid = s.get("low", 0.2), s.get("high", 99.95), s.get("mid", 95.0)
                    self.spin_scale.setValue(int(s.get("scale", 0.5) * 100))
                    self.combo_render.setCurrentText(s.get("mode", "AUTO"))
                    self.combo_bayer.setCurrentText(s.get("bayer", "AUTO"))
                    self.spin_fps.setValue(s.get("delay", 0.10))
                    self.chk_auto.setChecked(s.get("auto_stretch", True))
                    self.chk_tele.setChecked(s.get("telemetry", True))
                    self.chk_hist.setChecked(s.get("histogram", True))
                    self.spin_grid.setValue(s.get("grid_px", 30.0))
                    self.txt_notes.setText(s.get("notes", ""))
        except: pass

    def toggle_tele_hist(self):
        t = self.chk_tele.isChecked(); self.chk_hist.setEnabled(t)
        if not t: self.chk_hist.setChecked(False)
        self.check_state()

    def toggle_stretch_mode(self):
        is_auto = self.chk_auto.isChecked(); self.btn_manual.setEnabled(not is_auto); self.btn_manual.setStyleSheet("background-color: #333; color: #777;" if is_auto else "background-color: #2E7D32; color: white;"); self.check_state()

    def open_manual_stretch(self):
        active = [k for k, v in self.chk_dirs.items() if v.isChecked()]
        if not active: return
        ff = gather_and_validate_fits_multi(active); b = "NONE"
        try:
            with fits.open(ff[0], memmap=False) as h: b = h[0].header.get('BAYERPAT', 'NONE')
        except: pass
        dlg = ManualStretchDialog(ff[0], self.current_low, self.current_high, self.current_mid, b, b!="NONE", self)
        if dlg.exec(): self.current_low, self.current_high, self.current_mid = dlg.get_values(); self.chk_auto.setChecked(False); self.check_state()

    def check_state(self):
        active = [k for k, v in self.chk_dirs.items() if v.isChecked()]
        if not active: self.btn_act.setText("Process Video"); self.btn_act.setStyleSheet("background-color: #444;"); return
        ff = gather_and_validate_fits_multi(active); sp = find_best_siril_seq(active[0]); has_reg = bool(sp)
        try:
            with fits.open(ff[0], memmap=False) as hd:
                h, w = next(hh.data.shape[:2] for hh in hd if hh.data is not None)
                p = self.spin_scale.value()*0.01; self.lbl_res_info.setText(f"Res: {int(w*p)}x{int(h*p)}")
        except: pass
        ph = generate_params_hash(self.current_low, self.current_high, self.current_mid, self.combo_bayer.currentText(), self.combo_render.currentText()=="RGB", round(self.spin_scale.value()*0.01, 2), has_reg, self.chk_hist.isChecked(), self.chk_tele.isChecked())
        reg_p = os.path.join(self.cache_dir, "datasets_registry.json")
        if os.path.exists(reg_p):
            with open(reg_p, "r") as f:
                reg = json.load(f); all_ready = all(os.path.basename(pf) in reg and reg[os.path.basename(pf)].get("param_hash") == ph for pf in ff)
                if all_ready: self.btn_act.setText("Play Video"); self.btn_act.setStyleSheet("background-color: #327D2E;"); return
        self.btn_act.setText("Process Video"); self.btn_act.setStyleSheet("background-color: #B71C1C;")

    def on_action(self):
        if self.is_processing: self.worker.stop_requested = True; return
        active = [k for k, v in self.chk_dirs.items() if v.isChecked()]
        if not active: return
        ff = gather_and_validate_fits_multi(active)
        if self.btn_act.text() == "Play Video":
            reg_p = os.path.join(self.cache_dir, "datasets_registry.json")
            with open(reg_p, "r") as f:
                reg = json.load(f); p_v, m_v = [], []
                for pf in ff:
                    fn = os.path.basename(pf); entry = reg.get(fn)
                    if not entry: continue
                    m = entry["meta_item"].copy(); m["telemetry_active"] = self.chk_tele.isChecked(); m["hist_enabled"] = self.chk_hist.isChecked()
                    p_v.append(entry["png_path"]); m_v.append(m)
            run_opencv_player(p_v, m_v, self.spin_fps.value(), self.combo_render.currentText()=="RGB", self.chk_save.isChecked(), self.chk_poi.isChecked(), self.txt_notes.text(), self.chk_grid.isChecked(), self.spin_grid.value()); return
        self.is_processing = True; self.btn_act.setText("Stop"); sc = round(self.spin_scale.value()*0.01, 2)
        sp = find_best_siril_seq(active[0]); sd, hi = parse_siril_seq_data(sp)
        self.worker = PipelineWorker(ff, self.current_low, self.current_high, self.current_mid, self.combo_bayer.currentText(), self.combo_render.currentText()=="RGB", sc, self.cache_dir, sd, hi, bool(sp), self.chk_hist.isChecked(), self.chk_tele.isChecked())
        self.worker.progress_signal.connect(lambda c,t: (self.pbar.setMaximum(t), self.pbar.setValue(c), self.lbl_status.setText(f"Processing: {c}/{t}")))
        self.worker.finished_signal.connect(self.on_fin); self.worker.start()

    def on_fin(self, p, m, el, rgb):
        self.is_processing = False; self.lbl_status.setText("Idle"); self.last_duration = f"{el:.2f}s"
        if not self.worker.stop_requested and p:
            reg_p = os.path.join(self.cache_dir, "datasets_registry.json"); reg = {}
            if os.path.exists(reg_p):
                try:
                    with open(reg_p, "r") as f: reg = json.load(f)
                except: reg = {}
            active = [k for k, v in self.chk_dirs.items() if v.isChecked()]
            sp = find_best_siril_seq(active[0]); sc = round(self.spin_scale.value()*0.01, 2)
            ph = generate_params_hash(self.current_low, self.current_high, self.current_mid, self.combo_bayer.currentText(), rgb, sc, bool(sp), self.chk_hist.isChecked(), self.chk_tele.isChecked())
            for i in range(len(p)):
                fn = m[i]["filename"]; reg[fn] = {"png_path": p[i], "param_hash": ph, "meta_item": m[i]}
            reg["_last_ui_state"] = {
                "low": self.current_low, "high": self.current_high, "mid": self.current_mid, "scale": sc,
                "mode": self.combo_render.currentText(), "bayer": self.combo_bayer.currentText(),
                "delay": self.spin_fps.value(), "auto_stretch": self.chk_auto.isChecked(),
                "telemetry": self.chk_tele.isChecked(), "histogram": self.chk_hist.isChecked(),
                "grid_px": self.spin_grid.value(), "notes": self.txt_notes.text()
            }
            with open(reg_p, "w") as f: json.dump(reg, f, indent=4)
            run_opencv_player(p, m, self.spin_fps.value(), rgb, self.chk_save.isChecked(), self.chk_poi.isChecked(), self.txt_notes.text(), self.chk_grid.isChecked(), self.spin_grid.value())
        self.check_state(); self.update_stats()

    def update_stats(self):
        if not os.path.exists(self.cache_dir): os.makedirs(self.cache_dir, exist_ok=True)
        size = sum(f.stat().st_size for f in os.scandir(self.cache_dir) if f.is_file()); self.lbl_stats.setText(f"Last Duration: {self.last_duration} | Cache: {size // (1024*1024)} MB")
    def clear_cache(self):
        if os.path.exists(self.cache_dir): shutil.rmtree(self.cache_dir); os.makedirs(self.cache_dir); self.update_stats(); self.check_state()

def main():
    app = QApplication(sys.argv); pwd = os.getcwd(); paths = scan_for_lights_directories(pwd); cache = os.path.join(pwd, "astroview_cache"); os.makedirs(cache, exist_ok=True); gui = FITSImagePlayerGui(paths, cache); gui.show(); sys.exit(app.exec())

if __name__ == "__main__": main()
