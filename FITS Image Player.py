#!/usr/bin/env python3
"""
===============================================================================
🎯 FITS IMAGE PLAYER v1.0.0001 - NATIVE ASTROPHOTOGRAPHY STREAMING ENGINE
===============================================================================

DESCRIPTION:
This application operates as a frame playback control panel featuring an
integrated rendering engine designed to interface with Siril. It inherits
Siril's current Working Directory (PWD) to dynamically locate lights/ and
process/ folders, enabling the batch-processing of multiple target datasets
provided that the PWD is set to the common root directory where the dataset
subfolders reside; otherwise, it processes one dataset at a time. It applies
asynchronous multi-core non-linear midtone stretching to render frames smoothly
inside a dynamic canvas playback player.

Key features include localized relative path resolution, granular frame-by-frame
cache persistence, dynamic rendering of source FITS histograms, multi-core
OpenCV debayering, live tracking of Siril sub-frame metadata (HFR) from light_.seq,
and session video capturing with full-frame or region-of-interest (ROI) export.

DEPENDENCIES:
- PyQt6         (GUI Architecture and Hardware Display Resolution Scaling)
- opencv-python (High-performance Image Manipulation and WebM Video Recording)
- numpy         (Fast Matrix Operations and Percentile Calculations)
- astropy       (Atomic FITS Header and Data Matrix Extraction)

USAGE NOTE:
This script is engineered to be executed within Siril's working environment,
implicitly inheriting the runtime context and folder structures.
"""


import os
import sys

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
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np
from astropy.io import fits
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QSlider,
                             QPushButton, QGroupBox, QProgressBar, QComboBox,
                             QCheckBox, QDoubleSpinBox, QVBoxLayout, QHBoxLayout,
                             QGridLayout, QScrollArea)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRect
from PyQt6.QtGui import QImage, QPixmap, QPainter, QPen, QColor

# =============================================================================
# SECTION 1: SYSTEM UTILITIES & FILE PATH SERVICES
# =============================================================================

def get_desktop_resolution():
    """
    Retrieves the exact hardware resolution of the primary screen host
    using the active PyQt6 application screen instance. Fallback to 1080p.
    """
    try:
        app = QApplication.instance()
        if not app:
            return 1920, 1080
        primary_screen = app.primaryScreen()
        if primary_screen:
            geometry = primary_screen.geometry()
            width = geometry.width()
            height = geometry.height()
            if width > 0 and height > 0:
                return width, height
    except Exception as e:
        print(f"[System Sync] Warning, screen resolution detection failed: {e}")
    return 1920, 1080


def scan_for_lights_directories(root_path):
    """
    Scanner di directory confinato a 2 livelli ottimizzato per Linux e Windows.
    Normalizza i percorsi ed elimina i doppioni case-insensitive causati da Windows.
    """
    lights_dirs = []
    extensions = ('.fits', '.fit', '.FITS', '.FIT')

    if not root_path:
        return []

    # Normalizza il percorso radice per il sistema operativo corrente
    root_path = os.path.abspath(root_path)

    # Livello 1: Controllo se la cartella corrente è già 'lights'
    base_name = os.path.basename(root_path)
    if base_name.lower() == "lights":
        try:
            if any(f.lower().endswith(extensions) for f in os.listdir(root_path) if os.path.isfile(os.path.join(root_path, f))):
                return [root_path]
        except: pass

    # Livello 2: Scansione delle sottocartelle immediate (Ciano, Giallo, Magenta...)
    try:
        for item in os.listdir(root_path):
            full_path = os.path.join(root_path, item)
            if os.path.isdir(full_path):
                # Se la cartella analizzata non è essa stessa la cartella lights
                if item.lower() != "lights":
                    # Cerchiamo le varianti in modo case-insensitive
                    for sub_name in os.listdir(full_path):
                        if sub_name.lower() == "lights":
                            sub_lights_path = os.path.join(full_path, sub_name)
                            if os.path.isdir(sub_lights_path):
                                try:
                                    if any(f.lower().endswith(extensions) for f in os.listdir(sub_lights_path) if os.path.isfile(os.path.join(sub_lights_path, f))):
                                        lights_dirs.append(os.path.abspath(sub_lights_path))
                                except: pass
                else:
                    # Se siamo nella cartella principale e c'è una cartella 'lights' diretta
                    try:
                        if any(f.lower().endswith(extensions) for f in os.listdir(full_path) if os.path.isfile(os.path.join(full_path, f))):
                            lights_dirs.append(os.path.abspath(full_path))
                    except: pass
    except:
        pass

    # 🎯 TRUCCO CROSS-PLATFORM: Elimina i doppioni reali e virtuali normalizzando i percorsi in minuscolo
    seen = set()
    unique_dirs = []
    for d in lights_dirs:
        # Su Windows compariamo in minuscolo per collassare Ciano\Lights e Ciano\lights in un unico percorso
        normalized_key = os.path.normpath(d).lower() if os.name == 'nt' else os.path.normpath(d)
        if normalized_key not in seen:
            seen.add(normalized_key)
            unique_dirs.append(os.path.normpath(d)) # Mantiene il percorso originale formattato bene

    return sorted(unique_dirs)


def gather_and_validate_fits_multi(directory_list):
    """
    Gathers, filters, and sorts alphabetically all FITS files located
    within the target directory list to preserve temporal sequence.
    """
    valid_fits = []
    extensions = ('.fits', '.fit', '.FITS', '.FIT')
    for d in directory_list:
        if os.path.isdir(d):
            for file in sorted(os.listdir(d)):
                if file.lower().endswith(extensions):
                    valid_fits.append(os.path.join(d, file))
    return sorted(valid_fits)


def parse_filename(filename_str):
    """
    Semantic regex parser extracting the astronomical target name by splitting
    the string right before standard acquisition metadata tags.
    """
    cleaned = filename_str.strip()
    match_light = re.search(r'_(Light)_', cleaned, re.IGNORECASE)
    if match_light:
        parts = cleaned[:match_light.start()].split('_')
        return " ".join(parts), "Light"
    return "Unknown", "Light"


# =============================================================================
# SECTION 2: ASYNCHRONOUS PIPELINE BACKGROUND WORKER
# =============================================================================

class PipelineWorker(QThread):
    """
    Asynchronous QThread preventing GUI freezing during intensive
    multi-core FITS frame math and PNG pre-conversion routines.
    """
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal(list, list, float, bool)

    def __init__(self, fits_files, low, high, bayer, is_rgb, include_hist, scale, cache_dir, band, telemetry):
        super().__init__()
        self.fits_files = fits_files
        self.low = low
        self.high = high
        self.bayer = bayer
        self.is_rgb = is_rgb
        self.include_hist = include_hist
        self.scale = scale
        self.cache_dir = cache_dir
        self.band = band
        self.telemetry = telemetry
        self.stop_requested = False

    def run(self):
        """Executes the pre-conversion processing queue."""
        def update_cb(current, total):
            self.progress_signal.emit(current, total)

        start_time = time.time()
        png_vector, meta_log = pre_convert_fits_to_png(
            self.fits_files, self.low, self.high, self.bayer, self.is_rgb, self.include_hist,
            self.scale, self.cache_dir, self.band, self.telemetry,
            False, update_cb, lambda: self.stop_requested
        )
        elapsed = time.time() - start_time
        self.finished_signal.emit(png_vector, meta_log, elapsed, self.is_rgb)


# =============================================================================
# SECTION 3: CONTROL PANEL INTERFACE (GUI VIEW)
# =============================================================================

class FITSImagePlayerGui(QMainWindow):
    """
    Main interface control window managing user selection parameters,
    non-linear stretch sliders, sensor overrides, and processing triggers.
    """
    def __init__(self, detected_dirs, cache_directory, last_state_ref):
        super().__init__()
        self.detected_dirs = sorted(detected_dirs)
        self.cache_dir = cache_directory
        self.last_state = last_state_ref

        self.cached_files_vector = []
        self.metadata_log = []
        self.is_processing = False
        self.is_cached_ready = False

        self.setWindowTitle("FITS Image Player v1.0 - Control Panel")
        self.setFixedSize(640, 620)

        # Apply optimized flat dark styling theme
        self.setStyleSheet("QMainWindow, QWidget { background-color: #282828; color: #E0E0E0; font-family: Helvetica; } "
                           "QGroupBox { border: 1px solid #646464; margin-top: 10px; padding-top: 10px; font-weight: bold; } "
                           "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; } "
                           "QPushButton { background-color: #B71C1C; color: white; font-weight: bold; border-radius: 3px; }")

        self.setup_ui()
        self.update_cache_size()

    def setup_ui(self):
        """Initializes all interactive window frames and widgets."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 10, 15, 15)

        # Sezione 1: Target Folder Dataset Selection (CON BARRA DI SCORRIMENTO AUTOMATICA)
        group_dataset = QGroupBox(" Target Dataset Selection ")
        layout_dataset = QVBoxLayout(group_dataset)

        # 1. Creiamo l'area di scorrimento
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFixedHeight(120) # Blocca l'altezza massima del box dei filtri per non spingere giù lo stretch
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        # 2. Creiamo un contenitore interno per i checkbox
        scroll_widget = QWidget()
        scroll_widget.setStyleSheet("background-color: transparent;")
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(5)

        self.chk_dirs = {}
        if not self.detected_dirs:
            lbl_warn = QLabel("⚠ NO ASTROPHOTOGRAPHY DATASETS FOUND IN PWD")
            lbl_warn.setStyleSheet("color: #B71C1C; font-weight: bold;")
            scroll_layout.addWidget(lbl_warn)
        else:
            # Rimosso il limite [:4] per caricare TUTTI i filtri esistenti nel PC
            for d in self.detected_dirs:
                chk = QCheckBox(os.path.relpath(d, os.getcwd()))
                chk.stateChanged.connect(self.check_state)
                scroll_layout.addWidget(chk)
                self.chk_dirs[d] = chk
            scroll_layout.addStretch() # Spinge i checkbox verso l'alto se sono pochi

        # 3. Agganciamo il contenitore all'area di scorrimento e l'area al gruppo principale
        scroll_area.setWidget(scroll_widget)
        layout_dataset.addWidget(scroll_area)
        main_layout.addWidget(group_dataset)


        # Sezione 2: Non-linear Stretch Threshold Parameters
        group_stretch = QGroupBox(" Pre-Conversion Stretch Parameters ")
        grid_stretch = QGridLayout(group_stretch)

        self.lbl_black = QLabel(f"Black Floor (%): {float(self.last_state.get('p_low', 0.2))}")
        self.slide_black = QSlider(Qt.Orientation.Horizontal)
        self.slide_black.setRange(0, 200)
        self.slide_black.setValue(int(float(self.last_state.get('p_low', 0.2)) * 10))
        self.slide_black.valueChanged.connect(self.on_stretch_changed)
        grid_stretch.addWidget(self.lbl_black, 0, 0)
        grid_stretch.addWidget(self.slide_black, 0, 1)

        self.lbl_white = QLabel(f"White Ceiling (%): {float(self.last_state.get('p_high', 99.8))}")
        self.slide_white = QSlider(Qt.Orientation.Horizontal)
        self.slide_white.setRange(800, 1000)
        self.slide_white.setValue(int(float(self.last_state.get('p_high', 99.8)) * 10))
        self.slide_white.valueChanged.connect(self.on_stretch_changed)
        grid_stretch.addWidget(self.lbl_white, 1, 0)
        grid_stretch.addWidget(self.slide_white, 1, 1)
        main_layout.addWidget(group_stretch)

        # Sezione 3: Camera Sensor Overrides
        group_overrides = QGroupBox(" Camera Sensor Overrides ")
        layout_overrides = QHBoxLayout(group_overrides)

        layout_overrides.addWidget(QLabel("Render Mode:"))
        self.combo_render = QComboBox()
        self.combo_render.addItems(["AUTO", "MONO", "RGB"])
        self.combo_render.currentTextChanged.connect(self.check_state)
        layout_overrides.addWidget(self.combo_render)

        layout_overrides.addWidget(QLabel("Bayer Layout:"))
        self.combo_bayer = QComboBox()
        self.combo_bayer.addItems(["AUTO", "RGGB", "BGGR", "GBRG", "GRBG"])
        self.combo_bayer.currentTextChanged.connect(self.check_state)
        layout_overrides.addWidget(self.combo_bayer)

        layout_overrides.addWidget(QLabel("Timing:"))
        self.spin_fps = QDoubleSpinBox()
        self.spin_fps.setRange(0.01, 5.0)
        self.spin_fps.setSingleStep(0.05)
        self.spin_fps.setValue(0.10)
        layout_overrides.addWidget(self.spin_fps)
        main_layout.addWidget(group_overrides)

        # Sezione 4: Video Recorder and Caption Layout Configuration
        group_video = QGroupBox(" Video Options & Overlays Layout ")
        grid_video = QGridLayout(group_video)

        self.chk_save = QCheckBox("Save session as video file")
        self.chk_poi = QCheckBox("Record after POI selection")
        self.chk_band = QCheckBox("Enable bottom caption info band")
        self.chk_band.setChecked(True)
        self.chk_telemetry = QCheckBox("Show Gain/Exp/Pier")
        self.chk_telemetry.setChecked(True)
        self.chk_hfr = QCheckBox("Display light_.seq metrics (requires registration)")
        self.chk_hfr.setChecked(True)
        self.chk_hist = QCheckBox("Embed pre-stretch histogram")
        self.chk_hist.setChecked(True)

        grid_video.addWidget(self.chk_save, 0, 0)
        grid_video.addWidget(self.chk_poi, 0, 1)
        grid_video.addWidget(self.chk_band, 1, 0)
        grid_video.addWidget(self.chk_telemetry, 1, 1)
        grid_video.addWidget(self.chk_hfr, 2, 0)
        grid_video.addWidget(self.chk_hist, 2, 1)
        main_layout.addWidget(group_video)

        # Sezione 5: Engine Status Bars and Capacity Metric Output
        self.lbl_status = QLabel("Engine Status: Idle")
        main_layout.addWidget(self.lbl_status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("QProgressBar { border: 1px solid #646464; border-radius: 3px; text-align: center; } "
                                        "QProgressBar::chunk { background-color: #327D2E; }")
        main_layout.addWidget(self.progress_bar)

        # 🎯 KEYBOARD CONTROLS QUICK GUIDE
        self.lbl_shortcuts = QLabel("Keyboard Controls:\n[Space] Play/Pause  |  [< / Left] Prev Frame  |  [> / Right] Next Frame  |  [Esc/Q/X] Close")
        self.lbl_shortcuts.setStyleSheet("color: #00FFFF; font-size: 11px; font-weight: bold; margin-top: 5px;")
        main_layout.addWidget(self.lbl_shortcuts)

        self.lbl_cache = QLabel("Last Processing Duration: N/A | Cache Size: 0 MB")
        self.lbl_cache.setStyleSheet("color: #8C8C8C; font-size: 11px;")
        main_layout.addWidget(self.lbl_cache)


        # Sezione 6: Execution Action Controls
        layout_buttons = QHBoxLayout()
        self.btn_action = QPushButton("Process Video")
        self.btn_action.setFixedSize(180, 35)
        self.btn_action.clicked.connect(self.on_action_clicked)

        btn_close = QPushButton("Close")
        btn_close.setFixedSize(120, 35)
        btn_close.setStyleSheet("background-color: #505050; color: white;")
        btn_close.clicked.connect(self.close)

        layout_buttons.addWidget(self.btn_action, alignment=Qt.AlignmentFlag.AlignLeft)
        layout_buttons.addStretch()
        layout_buttons.addWidget(btn_close, alignment=Qt.AlignmentFlag.AlignRight)
        main_layout.addLayout(layout_buttons)

    def on_stretch_changed(self):
        """Updates text metric thresholds live as sliders move."""
        low = self.slide_black.value() / 10.0
        high = self.slide_white.value() / 10.0
        self.lbl_black.setText(f"Black Floor (%): {low}")
        self.lbl_white.setText(f"White Ceiling (%): {high}")
        self.check_state()

    def update_cache_size(self):
        """Calculates total disk weight consumed by the cache directory."""
        total_bytes = 0
        base_cache = os.path.join(os.getcwd(), "astroview_cache")
        if os.path.exists(base_cache):
            try:
                for entry in os.scandir(base_cache):
                    if entry.is_file(): total_bytes += entry.stat().st_size
            except: pass
        size_mb = total_bytes / (1024 * 1024)
        c_str = f"{size_mb / 1024:.2f} GB" if size_mb >= 1024 else f"{int(size_mb)} MB"
        self.lbl_cache.setText(f"Last Processing Duration: N/A | Cache Size: {c_str}")

    def check_state(self):
        """
        Scans cache map states to determine button functionality. Swaps UI behavior
        to instant 'Play Video' green state if selected files exist in register maps.
        """
        active_dirs = [k for k, v in self.chk_dirs.items() if v.isChecked()]
        if not active_dirs:
            self.btn_action.setText("Process Video")
            self.btn_action.setStyleSheet("background-color: #B71C1C; color: white;")
            self.is_cached_ready = False
            return

        fits_files = gather_and_validate_fits_multi(active_dirs)
        h_mode, h_bayer = inspect_fits_metadata_safely(fits_files)
        tgt_bayer = self.combo_bayer.currentText() if self.combo_bayer.currentText() != "AUTO" else h_bayer
        tgt_mode = self.combo_render.currentText() if self.combo_render.currentText() != "AUTO" else h_mode
        is_rgb = (tgt_mode == "RGB")

        auto_scale = 1.0
        if fits_files:
            try:
                with fits.open(fits_files[0], memmap=False) as hdul:
                    for hdu in hdul:
                        if hdu.data is not None and hdu.data.ndim >= 2:
                            h, w = hdu.data.shape[:2]
                            sw, sh = get_desktop_resolution()
                            auto_scale = min((sw - 180) / w, (sh - 300) / h)
                            if auto_scale >= 1.0: auto_scale = 1.0
                            break
            except: pass

        param_hash = generate_params_hash(self.slide_black.value()/10.0, self.slide_white.value()/10.0, tgt_bayer, is_rgb, auto_scale)
        local_reg = os.path.join(os.getcwd(), "astroview_cache", "datasets_registry.json")

        ready = 0
        if os.path.exists(local_reg):
            try:
                with open(local_reg, "r") as f:
                    reg = json.load(f)
                    for pf in fits_files:
                        fname = os.path.basename(pf)
                        if fname in reg and reg[fname].get("param_hash") == param_hash:
                            if os.path.exists(reg[fname].get("png_path", "")):
                                ready += 1
                            else: break
                        else: break
            except: pass

        if ready == len(fits_files) and len(fits_files) > 0:
            self.btn_action.setText("Play Video")
            self.btn_action.setStyleSheet("background-color: #327D2E; color: white;")
            self.is_cached_ready = True
        else:
            self.btn_action.setText("Process Video")
            self.btn_action.setStyleSheet("background-color: #B71C1C; color: white;")
            self.is_cached_ready = False

    def on_action_clicked(self):
        """Triggers asynchronous frame processing or launches the streaming player."""
        if self.is_processing:
            self.worker.stop_requested = True
            self.lbl_status.setText("Engine Status: Aborting...")
            return

        active_dirs = [k for k, v in self.chk_dirs.items() if v.isChecked()]
        if not active_dirs: return

        fits_files = gather_and_validate_fits_multi(active_dirs)
        if not fits_files: return

        # Instant playback bootstrapper if cache matches fully
        if self.is_cached_ready:
            local_reg = os.path.join(os.getcwd(), "astroview_cache", "datasets_registry.json")
            png_vector = []
            meta_log = []
            try:
                with open(local_reg, "r") as f:
                    reg = json.load(f)
                    for pf in fits_files:
                        fname = os.path.basename(pf)
                        if fname in reg:
                            png_vector.append(reg[fname]["png_path"])
                            m_item = reg[fname]["meta_item"].copy()
                            m_item["band_active"] = self.chk_band.isChecked()
                            m_item["telemetry_active"] = self.chk_telemetry.isChecked() if self.chk_band.isChecked() else False
                            meta_log.append(m_item)
            except: return

            if png_vector:
                run_opencv_player(png_vector, meta_log, self.spin_fps.value(), (self.combo_render.currentText() == "RGB"), self.chk_save.isChecked(), self.chk_poi.isChecked())
                return

        h_mode, h_bayer = inspect_fits_metadata_safely(fits_files)
        tgt_bayer = self.combo_bayer.currentText() if self.combo_bayer.currentText() != "AUTO" else h_bayer
        tgt_mode = self.combo_render.currentText() if self.combo_render.currentText() != "AUTO" else h_mode
        is_rgb = (tgt_mode == "RGB")

        sw, sh = get_desktop_resolution()
        h_raw, w_raw = 1080, 1920
        try:
            with fits.open(fits_files[0], memmap=False) as hdul:
                for hdu in hdul:
                    if hdu.data is not None and hdu.data.ndim >= 2:
                        h_raw, w_raw = hdu.data.shape[:2]
                        break
        except: pass

        scale = min((sw - 180) / w_raw, (sh - 300) / h_raw)
        if scale >= 1.0: scale = 1.0

        self.is_processing = True
        self.btn_action.setText("Stop")
        self.btn_action.setStyleSheet("background-color: #B71C1C; color: white;")

        self.worker = PipelineWorker(
            fits_files,
            self.slide_black.value() / 10.0,
            self.slide_white.value() / 10.0,
            tgt_bayer,
            is_rgb,
            self.chk_hist.isChecked(),
            scale,
            self.cache_dir,
            self.chk_band.isChecked(),
            self.chk_telemetry.isChecked()
        )
        self.worker.progress_signal.connect(self.on_pipeline_progress)
        self.worker.finished_signal.connect(self.on_pipeline_finished)
        self.worker.start()

    def on_pipeline_progress(self, current, total):
        """Updates progression bars and text gauges during processing."""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.lbl_status.setText(f"Processing frames: {current}/{total}")

    def on_pipeline_finished(self, png_vector, meta_log, elapsed, is_rgb):
        """Fires at worker completion, resetting gauges and spawning the playback window."""
        self.is_processing = False
        self.progress_bar.setValue(0)
        self.lbl_status.setText("Engine Status: Idle")
        self.check_state()

        total_bytes = 0
        base_cache = os.path.join(os.getcwd(), "astroview_cache")
        if os.path.exists(base_cache):
            try:
                for entry in os.scandir(base_cache):
                    if entry.is_file(): total_bytes += entry.stat().st_size
            except: pass
        size_mb = total_bytes / (1024 * 1024)
        c_str = f"{size_mb / 1024:.2f} GB" if size_mb >= 1024 else f"{int(size_mb)} MB"

        self.lbl_cache.setText(f"Last Processing Duration: {elapsed:.2f} s | Cache Size: {c_str}")

        if not png_vector or self.worker.stop_requested: return

        for m in meta_log:
            m["band_active"] = self.chk_band.isChecked()
            m["telemetry_active"] = self.chk_telemetry.isChecked() if self.chk_band.isChecked() else False

        run_opencv_player(png_vector, meta_log, self.spin_fps.value(), is_rgb, self.chk_save.isChecked(), self.chk_poi.isChecked())
# =============================================================================
# SECTION 4: SCIENTIFIC ANALYSIS & FITS DATA MATRIX PROCESSING
# =============================================================================

def execute_debayer_safely(mono_img, pattern_str):
    """
    Executes hardware-accelerated demosaicing conversions using C++ OpenCV routines.
    Ensures safe array data casting to unsigned 16-bit to prevent core calculation crashes.
    """
    p = pattern_str.upper()
    if p == "NONE" or not p:
        if mono_img.ndim == 2:
            return cv2.cvtColor(mono_img.astype(np.uint16), cv2.COLOR_GRAY2BGR)
        return mono_img

    try:
        # OpenCV bayer conversion routines strictly demand integer uint8 or uint16 topologies
        img_uint16 = np.clip(mono_img, 0, 65535).astype(np.uint16)

        if p == "RGGB": return cv2.cvtColor(img_uint16, cv2.COLOR_BayerBG2BGR)
        if p == "BGGR": return cv2.cvtColor(img_uint16, cv2.COLOR_BayerRG2BGR)
        if p == "GBRG": return cv2.cvtColor(img_uint16, cv2.COLOR_BayerGR2BGR)
        if p == "GRBG": return cv2.cvtColor(img_uint16, cv2.COLOR_BayerGB2BGR)
    except Exception as e:
        print(f"[Core Engine] OpenCV debayer pattern conversion exception raised: {e}")

    # Safe grayscale array recovery fallback to maintain thread pipeline execution
    if mono_img.ndim == 2:
        return cv2.cvtColor(mono_img.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    return mono_img


def inspect_fits_metadata_safely(file_list):
    """
    Scans the header dictionary keys of the first frame file in the sequence
    to automatically detect channel profiles and sensor Bayer matrices.
    """
    mode, bayer = "MONO", "NONE"
    if not file_list: return mode, bayer
    try:
        with fits.open(file_list[0], mode='readonly', memmap=False) as hdul:
            for hdu in hdul:
                if hdu.data is not None and hdu.data.ndim >= 2:
                    header = hdu.header
                    bayer_key = ""
                    if 'BAYERPAT' in header: bayer_key = str(header['BAYERPAT']).strip().upper()
                    elif 'COLORTYP' in header: bayer_key = str(header['COLORTYP']).strip().upper()
                    if bayer_key:
                        for pat in ["RGGB", "BGGR", "GBRG", "GRBG"]:
                            if pat in bayer_key: return "RGB", pat
    except: pass
    return mode, bayer


def draw_advanced_histogram(raw_fits_data, bayer_pat, is_rgb, width=220, height=75, save_path=None):
    """
    Generates a linear distribution histogram calculation over individual raw channels
    before applying destructive non-linear stretching. Draws directly to an OpenCV matrix canvas.
    """
    hist_canvas = np.zeros((height, width, 3), dtype=np.uint8)
    plot_w, plot_h = width - 25, height - 15
    cv2.rectangle(hist_canvas, (0, 0), (plot_w + 1, plot_h + 1), (255, 255, 255), 1)

    img_float = np.nan_to_num(raw_fits_data).astype(np.float32)
    f_min, f_max = np.min(img_float), np.max(img_float)
    if f_max - f_min == 0: return hist_canvas

    f_med = np.median(img_float)
    img_8b = ((img_float - f_min) / (f_max - f_min) * 255).astype(np.uint8)
    colors_to_draw = []

    if is_rgb:
        img_bgr = execute_debayer_safely(img_8b.astype(np.uint16) * 256, bayer_pat)
        img_bgr = (img_bgr / 256).astype(np.uint8)
        colors_to_draw = [
            (0, cv2.calcHist([img_bgr], [0], None, [256], [0, 256])),
            (1, cv2.calcHist([img_bgr], [1], None, [256], [0, 256])),
            (2, cv2.calcHist([img_bgr], [2], None, [256], [0, 256]))
        ]
    else:
        colors_to_draw = [(-1, cv2.calcHist([img_8b], [0], None, [256], [0, 256]))]

    max_val = max(1.0, max(np.max(h_arr) for _, h_arr in colors_to_draw))
    for c_idx, h_arr in colors_to_draw:
        if c_idx == 0: color = (255, 0, 0)
        elif c_idx == 1: color = (0, 255, 0)
        elif c_idx == 2: color = (0, 0, 255)
        else: color = (255, 255, 255)

        for i in range(256):
            x_pos = int((i / 256.0) * plot_w)
            y_val = int((h_arr.item(i) / max_val) * (plot_h - 2))
            if x_pos < plot_w and y_val > 0:
                cv2.line(hist_canvas, (x_pos + 1, plot_h), (x_pos + 1, plot_h - y_val), color, 1, cv2.LINE_AA)

    f_font = cv2.FONT_HERSHEY_SIMPLEX
    lbl_scale = 0.24
    lbl_color = (160, 160, 160)
    cv2.putText(hist_canvas, f"{int(f_min)}", (0, height - 3), f_font, lbl_scale, lbl_color, 1, cv2.LINE_AA)
    cv2.putText(hist_canvas, f"{int(f_med)}", (int(plot_w / 2) - 12, height - 3), f_font, lbl_scale, lbl_color, 1, cv2.LINE_AA)
    cv2.putText(hist_canvas, f"{int(f_max)}", (plot_w - 20, height - 3), f_font, lbl_scale, lbl_color, 1, cv2.LINE_AA)

    if save_path: cv2.imwrite(save_path, hist_canvas)
    return hist_canvas


# =============================================================================
# SECTION 5: GRANULAR MULTI-THREAD CACHE AND METADATA MANAGER
# =============================================================================

def generate_params_hash(p_low, p_high, bayer_pat, is_rgb, auto_scale):
    """Composes an MD5 checksum based on configuration profiles to identify identical states."""
    hash_str = f"{p_low}_{p_high}_{bayer_pat}_{is_rgb}_{auto_scale}"
    return hashlib.md5(hash_str.encode()).hexdigest()[:8]


def safe_load_json_with_retry(file_path, retries=5, delay=0.02):
    """Reads JSON files using consecutive micro-delay re-entries to bypass pool thread-locks."""
    if not os.path.exists(file_path):
        return {}
    for _ in range(retries):
        try:
            with open(file_path, "r") as f:
                content = f.read().strip()
                if content: return json.loads(content)
        except (json.JSONDecodeError, IOError):
            time.sleep(delay)
    return {}


def _process_single_frame_worker(task_args):
    """
    High-efficiency structural frame thread worker mapping native execution operations:
    Linear Histogram Canvas -> Debayer -> Flip Alignment -> PRE-STRETCH RESIZE -> Non-linear Math -> Save PNG.
    """
    fits_path, p_low, p_high, bayer_pat, is_rgb, auto_scale, base_cache_dir, param_hash, initial_pier_side, force_rebuild = task_args

    fname_fits = os.path.basename(fits_path)
    base_name = fname_fits
    for ext in ['.fits', '.fit', '.FITS', '.FIT']:
        if base_name.endswith(ext):
            base_name = base_name.replace(ext, '')
            break

    png_name = f"{base_name}_{param_hash}.png"
    hist_name = f"hist_{base_name}_{param_hash}.png"
    out_name = os.path.join(base_cache_dir, png_name)
    out_hist = os.path.join(base_cache_dir, hist_name)

    try:
        with fits.open(fits_path, memmap=False) as hdul:
            header, raw_data = None, None
            for hdu in hdul:
                if hdu.data is not None and hdu.data.ndim >= 2:
                    raw_data, header = hdu.data, hdu.header
                    break

            if raw_data is None or header is None:
                return None

            # Track meridian flip information
            rel_pier = header.get('PIERSIDE', 'Unknown').strip().upper()
            if rel_pier == 'UNKNOWN' and 'HA' in header:
                try: rel_pier = "WEST" if float(header['HA']) > 0 else "EAST"
                except: pass

            date_obs = header.get('DATE-OBS', 'N/A')
            exp_time = f"{float(header.get('EXPTIME', header.get('EXPOSURE', 0))):.1f}s"
            gain_val = header.get('GAIN', header.get('CCD-GAIN', 'N/A'))
            gain_str = f"{int(gain_val)}" if gain_val != 'N/A' else "N/A"
            target, _ = parse_filename(fname_fits)
            flip_str = " (Riflipped)" if (initial_pier_side and rel_pier != 'UNKNOWN' and rel_pier != initial_pier_side) else ""

            # Save the pre-stretch histogram matrix map
            draw_advanced_histogram(raw_data, bayer_pat, is_rgb, width=220, height=75, save_path=out_hist)

            meta_item = {
                "fits_real_name": fname_fits, "filename": fname_fits, "date_obs": date_obs,
                "target": target or "N/A", "gain": gain_str, "exp_time": exp_time,
                "pier_side": "East" if rel_pier == "EAST" else "West" if rel_pier == "WEST" else "Unknown",
                "flip_str": flip_str, "hfr": "Calc...",
                "hist_disk_path": out_hist, "local_cache_dir": base_cache_dir
            }

            if os.path.exists(out_name) and os.path.exists(out_hist) and not force_rebuild:
                return fname_fits, out_name, meta_item

            img_data = np.nan_to_num(raw_data)
            img_data = execute_debayer_safely(img_data, bayer_pat)
            img_data = img_data.astype(np.float32)

            if flip_str != "":
                img_data = cv2.rotate(img_data, cv2.ROTATE_180)

            # Pre-stretch downscaling to relieve cpu computation burdens
            if auto_scale != 1.0:
                h_orig, w_orig = img_data.shape[:2]
                img_data = cv2.resize(img_data, (int(w_orig * auto_scale), int(h_orig * auto_scale)), interpolation=cv2.INTER_AREA)

            p_min, p_max = np.percentile(img_data, (p_low, p_high))
            if p_max - p_min != 0:
                img_data = np.clip((img_data - p_min) / (p_max - p_min) * 255, 0, 255).astype(np.uint8)
            else:
                img_data = np.zeros_like(img_data, dtype=np.uint8)

            cv2.imwrite(out_name, img_data)
            return fname_fits, out_name, meta_item
    except Exception as e:
        print(f"[Worker Task Error] Failed processing file {fname_fits}: {e}")
        return None


def pre_convert_fits_to_png(file_list, p_low, p_high, bayer_pat, is_rgb, include_hist, auto_scale, cache_directory, band_active=True, telemetry_active=True, force_rebuild=False, progress_callback=None, check_stop_callback=None):
    """
    Main background manager splitting operations using natural alphanumeric sorting.
    Skips already generated frame caches on selective filtering states to boost performance.
    """
    generated_pngs = []
    metadata_log = []
    param_hash = generate_params_hash(p_low, p_high, bayer_pat, is_rgb, auto_scale)

    base_cache_dir = os.path.join(os.getcwd(), "astroview_cache")
    os.makedirs(base_cache_dir, exist_ok=True)

    global_reg_path = os.path.join(base_cache_dir, "datasets_registry.json")
    datasets_registry = safe_load_json_with_retry(global_reg_path)

    # Implement non-destructive human natural sorting key to prevent array index scrambling
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

    file_list = sorted(file_list, key=natural_sort_key)

    try:
        _, _, free = shutil.disk_usage(base_cache_dir)
        if (free / (1024 * 1024)) < 500.0:
            print("[Storage Notification] Low space available (<500 MB).")
            return [], []
    except: pass

    initial_pier_side = None
    if file_list and len(file_list) > 0:
        try:
            with fits.open(file_list[0], memmap=False) as hdul:
                for hdu in hdul:
                    if hdu.data is not None and hdu.data.ndim >= 2:
                        initial_pier_side = hdu.header.get('PIERSIDE', 'Unknown').strip().upper()
                        if initial_pier_side == 'UNKNOWN' and 'HA' in hdu.header:
                            initial_pier_side = "WEST" if float(hdu.header['HA']) > 0 else "EAST"
                        break
        except: pass

    cached_ready_map = {}
    pending_tasks_files = []

    # Map files and isolate ready items from processing queue
    for fits_path in file_list:
        fname = os.path.basename(fits_path)
        if not force_rebuild and fname in datasets_registry and datasets_registry[fname].get("param_hash") == param_hash:
            png_f = datasets_registry[fname].get("png_path", "")
            if os.path.exists(png_f):
                cached_ready_map[fname] = (png_f, datasets_registry[fname].get("meta_item", {}).copy())
                continue
        pending_tasks_files.append(fits_path)

    # Shortcut return if entire selection is fully pre-cached
    if not pending_tasks_files and len(file_list) > 0:
        for fits_path in file_list:
            fname = os.path.basename(fits_path)
            png_f, m_item = cached_ready_map[fname]
            generated_pngs.append(png_f)
            metadata_log.append(m_item)
        if progress_callback: progress_callback(len(file_list), len(file_list))
        return generated_pngs, metadata_log

    tasks = [
        (f, p_low, p_high, bayer_pat, is_rgb, auto_scale, base_cache_dir, param_hash, initial_pier_side, force_rebuild)
        for f in pending_tasks_files
    ]

    total_needed = len(pending_tasks_files)
    completed_count = len(file_list) - total_needed

    if progress_callback:
        progress_callback(completed_count, len(file_list))

    # Parallel Open-MP style thread mapping
    if tasks:
        with ThreadPoolExecutor(max_workers=None) as executor:
            results = executor.map(_process_single_frame_worker, tasks)

            for res in results:
                if check_stop_callback and check_stop_callback():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                if res is not None:
                    fname_fits, out_name, meta_item = res
                    cached_ready_map[fname_fits] = (out_name, meta_item)
                    datasets_registry[fname_fits] = {
                        "param_hash": param_hash, "png_path": out_name, "meta_item": meta_item
                    }

                completed_count += 1
                if progress_callback: progress_callback(completed_count, len(file_list))

    # Recompose array objects to respect initial natural sorted timelines
    for fits_path in file_list:
        fname = os.path.basename(fits_path)
        if fname in cached_ready_map:
            png_f, m_item = cached_ready_map[fname]
            generated_pngs.append(png_f)
            metadata_log.append(m_item)

    try:
        with open(global_reg_path, "w") as f_ptr:
            json.dump(datasets_registry, f_ptr, indent=4)
    except: pass

    if progress_callback: progress_callback(len(file_list), len(file_list))
    return generated_pngs, metadata_log


# =============================================================================
# SECTION 6: ANALYTICAL VIDEO STREAM PLAYER FRAMEWORK
# =============================================================================

class AstroViewPlayerWindow(QMainWindow):
    """
    Sub-window stream player managing linear double-click Zoom boxes, live text captions,
    WebM recording output pipelines, and native physical Siril .seq index parsing.
    """
    def __init__(self, cached_png_paths, metadata_vector, fps_value, is_rgb_forced, save_video, record_after_poi):
        super().__init__()
        self.cached_png_paths = cached_png_paths
        self.metadata_vector = metadata_vector
        self.fps_value = fps_value
        self.is_rgb_forced = is_rgb_forced
        self.save_video = save_video
        self.record_after_poi = record_after_poi

        self.total_frames = len(cached_png_paths)
        self.idx = 0
        self.is_paused = False

        self.siril_hfr_vector = {}
        self.parse_siril_sequence_file()

        self.crop_box = None
        self.is_cropped = False
        self.is_drawing = False
        self.ix, self.iy = -1, -1
        self.cx_curr, self.cy_curr = -1, -1

        self.video_writer = None
        self.video_writer_w = 0
        self.video_writer_h = 0
        self.has_started_recording = False
        self.frames_recorded = 0
        self.output_fps = 1.0 / fps_value if fps_value > 0 else 10.0
        if self.output_fps > 30.0: self.output_fps = 30.0

        img_init = cv2.imread(self.cached_png_paths[0])
        self.h_raw, self.w_raw = (1080, 1920) if img_init is None else img_init.shape[:2]

        self.setWindowTitle("FITS Image Player Stream Screen")
        self.video_widget = QWidget()
        self.setCentralWidget(self.video_widget)

        self.band_h = 85 if self.metadata_vector[0].get("band_active", True) else 0
        self.setFixedSize(self.w_raw, self.h_raw + self.band_h)

        self.timer = QTimer()
        self.timer.timeout.connect(self.play_next_frame)
        self.timer.start(int(self.fps_value * 1000) if self.fps_value > 0 else 100)

        self.video_widget.setMouseTracking(True)
        self.video_widget.mousePressEvent = self.on_mouse_press
        self.video_widget.mouseMoveEvent = self.on_mouse_move
        self.video_widget.mouseReleaseEvent = self.on_mouse_release
        self.video_widget.mouseDoubleClickEvent = self.on_mouse_double_click
        self.video_widget.paintEvent = self.on_paint_event

        self.current_q_img = None
        self.prepare_current_frame() # Genera subito il primo frame

        self.timer = QTimer()
        self.timer.timeout.connect(self.play_next_frame)
        self.timer.start(int(self.fps_value * 1000) if self.fps_value > 0 else 100)


    def find_siril_sequence_file(self):
        """Locates the fixed light_.seq file down from Siril's working folder."""
        try:
            pwd_dir = os.getcwd()
            candidate_nested = os.path.join(pwd_dir, "lights", "process", "light_.seq")
            if os.path.exists(candidate_nested): return candidate_nested

            candidate_nested_cap = os.path.join(pwd_dir, "lights", "process", "light_.seq")
            if os.path.exists(candidate_nested_cap): return candidate_nested_cap

            candidate_filter_root = os.path.join(pwd_dir, "process", "light_.seq")
            if os.path.exists(candidate_filter_root): return candidate_filter_root

            if self.metadata_vector and len(self.metadata_vector) > 0:
                filename_str = self.metadata_vector[0].get("fits_real_name", "").lower()
                for item in os.listdir(pwd_dir):
                    sub_dir = os.path.join(pwd_dir, item)
                    if os.path.isdir(sub_dir):
                        if item.lower() in ["astroview_cache", "process", "lights"]: continue
                        if item.lower() in filename_str:
                            seq_down = os.path.join(sub_dir, "process", "light_.seq")
                            if os.path.exists(seq_down): return seq_down
        except: pass
        return None

    def parse_siril_sequence_file(self):
        """Extracts and scales FWHM metrics from light_.seq aligning rows 1:1 with player frame loops."""
        self.siril_hfr_vector.clear()
        seq_path = self.find_siril_sequence_file()
        if not seq_path: return
        try:
            with open(seq_path, "r", errors="ignore") as f:
                lines = f.readlines()

            frame_index = 0
            has_valid_data = False  # Diventa True solo se troviamo reali righe 'R' con dati FWHM

            for line in lines:
                line_str = line.strip()

                # Intercetta le righe di registrazione R (che in questo set scarno non ci sono)
                if re.match(r'^R[0-9]?[\s\t]+', line_str):
                    line_str = line_str.replace(",", ".")
                    parts = line_str.split()

                    if len(parts) >= 3:
                        try:
                            fwhm_x = float(parts[1])
                            fwhm_y = float(parts[2])
                            if fwhm_x > 0 and fwhm_y > 0:
                                fwhm_media = np.sqrt(fwhm_x * fwhm_y)
                                self.siril_hfr_vector[frame_index] = f"{fwhm_media * 0.52:.2f}px"
                                has_valid_data = True
                            else:
                                self.siril_hfr_vector[frame_index] = "N/A"
                        except ValueError:
                            self.siril_hfr_vector[frame_index] = "N/A"
                    else:
                        self.siril_hfr_vector[frame_index] = "N/A"
                    frame_index += 1

            # --- APPLICA IL REFRESH IN RAM E SCRIVE SUL JSON DELLA CACHE ---
            if self.metadata_vector:
                base_cache_dir = os.path.join(os.getcwd(), "astroview_cache")
                global_reg_path = os.path.join(base_cache_dir, "datasets_registry.json")

                # Carica il registro JSON usando la funzione di rientro sicuro dello script
                datasets_registry = safe_load_json_with_retry(global_reg_path)
                json_updated = False

                for i, meta in enumerate(self.metadata_vector):
                    fname = meta.get("filename", "")
                    match_num = re.search(r'_(\d+)\.(fits|fit)$', fname, re.IGNORECASE)
                    seq_target_idx = int(match_num.group(1)) - 1 if match_num else i

                    # Se il file .seq esiste ma non ha i dati di allineamento delle stelle
                    if not has_valid_data:
                        real_hfr = "no light_.seq info"
                    else:
                        real_hfr = self.siril_hfr_vector.get(seq_target_idx, "N/A")

                    # Aggiorna la telemetria immediata per il playback corrente (RAM)
                    meta["hfr"] = real_hfr

                    # Rimuove "Calc..." dal file JSON su disco inserendo lo stato reale della sessione
                    if fname in datasets_registry:
                        if datasets_registry[fname]["meta_item"].get("hfr") != real_hfr:
                            datasets_registry[fname]["meta_item"]["hfr"] = real_hfr
                            json_updated = True

                # Salva le modifiche sul disco se necessario
                if json_updated:
                    try:
                        with open(global_reg_path, "w") as f_ptr:
                            json.dump(datasets_registry, f_ptr, indent=4)
                    except Exception as json_err:
                        print(f"[Debug Cache JSON] Errore di scrittura HFR: {json_err}")
            # --------------------------------------------------------

        except Exception as e:
            print(f"[Sequence Link Exception] Failed indexing sequence items: {e}")

    def closeEvent(self, event):
        """Wipes memory spaces and structures upon interface exit."""
        self.timer.stop()
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        self.siril_hfr_vector.clear()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space: self.is_paused = not self.is_paused
        elif event.key() in [Qt.Key.Key_Right, Qt.Key.Key_Greater]:
            self.is_paused = True
            self.idx = (self.idx + 1) % self.total_frames
            self.prepare_current_frame()
            self.update()
        elif event.key() in [Qt.Key.Key_Left, Qt.Key.Key_Less]:
            self.is_paused = True
            self.idx = (self.idx - 1 + self.total_frames) % self.total_frames
            self.prepare_current_frame()
            self.update()
        elif event.key() in [Qt.Key.Key_Escape, Qt.Key.Key_Q, Qt.Key.Key_X]: self.close()

    def play_next_frame(self):
        if not self.is_paused and not self.is_drawing:
            self.idx = (self.idx + 1) % self.total_frames
            self.prepare_current_frame()
            self.update()

    def on_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_drawing = True
            self.ix, self.iy = event.position().x(), event.position().y()
            self.cx_curr, self.cy_curr = self.ix, self.iy

    def on_mouse_move(self, event):
        if self.is_drawing:
            self.cx_curr, self.cy_curr = event.position().x(), event.position().y()
            self.update()

    def on_mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_drawing:
            self.is_drawing = False
            x1, y1 = int(min(self.ix, event.position().x())), int(min(self.iy, event.position().y()))
            x2, y2 = int(max(self.ix, event.position().x())), int(max(self.iy, event.position().y()))
            if (x2 - x1) < 5 or (y2 - y1) < 5: return
            center_x, center_y = (x1 + x2) // 2, (y1 + y2) // 2
            w_sel, h_sel = max(200, min(self.w_raw // 2, x2 - x1)), max(200, min(self.h_raw // 2, y2 - y1))
            new_x1 = max(0, min(self.w_raw - w_sel, center_x - w_sel // 2))
            new_y1 = max(0, min(self.h_raw - h_sel, center_y - h_sel // 2))
            self.crop_box = [new_x1, new_y1, new_x1 + w_sel, new_y1 + h_sel]
            self.is_cropped = True
            self.prepare_current_frame()
            self.update()

    def on_mouse_double_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_cropped = False
            self.crop_box = None
            self.prepare_current_frame()
            self.update()

    def prepare_current_frame(self):
        """Carica l'immagine da disco e prepara la QImage isolandola dal paintEvent."""
        # 1. Carichiamo l'immagine base (Manteniamo self.frame_base visibile per il videoregistratore)
        self.frame_base = cv2.imread(self.cached_png_paths[self.idx])
        if self.frame_base is None:
            return

        # 2. Gestione del Crop ROI originale (Manteniamo self.frame_active visibile a livello di istanza)
        if self.is_cropped and self.crop_box is not None:
            x1, y1, x2, y2 = self.crop_box
            sub = self.frame_base[y1:y2, x1:x2]
            self.frame_active = cv2.resize(sub, (self.w_raw, self.h_raw), interpolation=cv2.INTER_AREA)
        else:
            self.frame_active = self.frame_base.copy()

        # 3. Conversione dello spazio colore per la GUI
        frame_rgb = cv2.cvtColor(self.frame_active, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape

        # 4. Generiamo la QImage facendo una copia profonda per svuotare i puntatori volatili di OpenCV
        self.current_q_img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()

    def on_paint_event(self, event):
        painter = QPainter(self.video_widget)

        # Usiamo l'immagine pre-allocata senza allocare nuova memoria RAM
        if self.current_q_img is not None:
            painter.drawImage(0, 0, self.current_q_img)

        # Recupero dei metadati originali dello script
        meta = self.metadata_vector[self.idx]

        current_hfr = meta.get("hfr", "N/A")

        if self.band_h > 0:
            painter.fillRect(0, self.h_raw, self.w_raw, self.band_h, QColor("#000000"))
            painter.setPen(QColor("#F0F0F0"))
            painter.drawText(15, self.h_raw + 22, f"File: {meta.get('filename', 'N/A')}")
            painter.setPen(QColor("#00FFFF"))
            painter.drawText(15, self.h_raw + 42, f"Date: {meta.get('date_obs', 'N/A')}")
            painter.setPen(QColor("#F0F0F0"))
            painter.drawText(15, self.h_raw + 62, f"Tgt: {meta.get('target', 'N/A')}")
            painter.drawText(15, self.h_raw + 82, f"Frame: {self.idx + 1}/{self.total_frames}")

            if meta.get("telemetry_active", True):
                x_center = int(self.w_raw * 0.44)
                painter.setPen(QColor("#F0F0F0"))
                painter.drawText(x_center, self.h_raw + 22, f"Gain: {meta.get('gain', 'N/A')}")
                painter.drawText(x_center, self.h_raw + 42, f"Exp: {meta.get('exp_time', 'N/A')}")
                painter.drawText(x_center, self.h_raw + 62, f"Pier: {meta.get('pier_side', 'Unknown')}{meta.get('flip_str', '')}")
                painter.setPen(QColor("#00FF00") if "px" in current_hfr else QColor("#A0A0A0"))
                painter.drawText(x_center, self.h_raw + 82, f"HFR: {current_hfr}")

            if "hist_disk_path" in meta:
                hist_box = cv2.imread(meta["hist_disk_path"])
                if hist_box is not None:
                    hist_rgb = cv2.cvtColor(hist_box, cv2.COLOR_BGR2RGB)
                    hh, hw, hch = hist_rgb.shape
                    # Left only the correct format to prevent the crash
                    q_hist = QImage(hist_rgb.data, hw, hh, hch * hw, QImage.Format.Format_RGB888)

                    painter.drawImage(self.w_raw - hw - 15, self.h_raw + (self.band_h - hh) // 2, q_hist)


        # Draws the interactive cyanide selection crop bounding box
        if self.is_drawing:
            pen = QPen(QColor("#00FFFF"), 1, Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.drawRect(QRect(int(self.ix), int(self.iy), int(self.cx_curr - self.ix), int(self.cy_curr - self.iy)))

        # WebM Video Recorder RAM-to-Disk implementation
        frame_base = self.frame_base
        frame_active = self.frame_active

        if self.save_video:
            should_record = True
            if self.record_after_poi and not self.is_cropped:
                should_record = False

            if should_record:
                render_h, render_w = self.h_raw + self.band_h, self.w_raw
                if self.video_writer is not None and (self.video_writer_w != render_w or self.video_writer_h != render_h):
                    self.video_writer.release()
                    self.video_writer = None

                if self.video_writer is None:
                    local_cache = meta.get("local_cache_dir", "astroview_cache")
                    target_root_dir = os.path.dirname(local_cache)
                    v_name = f"fits_player_capture_{int(time.time())}.webm"
                    v_filename = os.path.join(target_root_dir, v_name)

                    fourcc = cv2.VideoWriter_fourcc(*'VP08')
                    self.video_writer = cv2.VideoWriter(v_filename, fourcc, self.output_fps, (render_w, render_h))
                    self.video_writer_w, self.video_writer_h = render_w, render_h

                if self.band_h > 0:
                    band_mat = np.zeros((self.band_h, self.w_raw, 3), dtype=np.uint8)
                    f_scale = max(0.38, min(0.42, self.w_raw / 1200.0))
                    cv2.putText(band_mat, f"File: {meta.get('filename', 'N/A')}", (15, 22), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (240, 240, 240), 1, cv2.LINE_AA)
                    cv2.putText(band_mat, f"Date: {meta.get('date_obs', 'N/A')}", (15, 42), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (255, 255, 0), 1, cv2.LINE_AA)
                    cv2.putText(band_mat, f"Tgt: {meta.get('target', 'N/A')}", (15, 62), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (240, 240, 240), 1, cv2.LINE_AA)
                    cv2.putText(band_mat, f"Frame: {self.idx + 1}/{self.total_frames}", (15, 82), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (240, 240, 240), 1, cv2.LINE_AA)

                    if meta.get("telemetry_active", True):
                        x_pos_gain = int(self.w_raw * 0.44)
                        cv2.putText(band_mat, f"Gain: {meta.get('gain', 'N/A')}", (x_pos_gain, 22), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (240, 240, 240), 1, cv2.LINE_AA)
                        cv2.putText(band_mat, f"Exp: {meta.get('exp_time', 'N/A')}", (x_pos_gain, 42), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (240, 240, 240), 1, cv2.LINE_AA)
                        cv2.putText(band_mat, f"Pier: {meta.get('pier_side', 'Unknown')}{meta.get('flip_str', '')}", (x_pos_gain, 62), cv2.FONT_HERSHEY_SIMPLEX, f_scale, (240, 240, 240), 1, cv2.LINE_AA)
                        h_bgr = (0, 255, 0) if "px" in current_hfr else (160, 160, 160)
                        cv2.putText(band_mat, f"HFR: {current_hfr}", (x_pos_gain, 82), cv2.FONT_HERSHEY_SIMPLEX, f_scale, h_bgr, 1, cv2.LINE_AA)

                    if "hist_disk_path" in meta:
                        hist_box = cv2.imread(meta["hist_disk_path"])
                        if hist_box is not None:
                            hh, hw = hist_box.shape[:2]
                            by = (self.band_h - hh) // 2
                            band_mat[by:by+hh, self.w_raw - hw - 15:self.w_raw - 15] = hist_box

                    frame_for_video = np.vstack([frame_active, band_mat])
                else:
                    frame_for_video = frame_active.copy()

                if self.is_drawing:
                    cv2.rectangle(frame_for_video, (int(self.ix), int(self.iy)), (int(self.cx_curr), int(self.cy_curr)), (255, 255, 0), 1)

                self.video_writer.write(frame_for_video)
                self.frames_recorded += 1

                if self.has_started_recording and self.frames_recorded >= self.total_frames:
                    self.save_video = False
                    self.video_writer.release()
                    self.video_writer = None


def run_opencv_player(cached_png_paths, metadata_vector, fps_value, is_rgb_forced, save_video=False, record_after_poi=False):
    """Global bootstrapper enforcing clean window replacement and parsing synchronization."""
    global player_window
    if 'player_window' in globals() and player_window is not None:
        try:
            player_window.close()
        except:
            pass
        player_window = None

    player_window = AstroViewPlayerWindow(cached_png_paths, metadata_vector, fps_value, is_rgb_forced, save_video, record_after_poi)
    player_window.parse_siril_sequence_file()
    player_window.show()


# =============================================================================
# SECTION 6: APPLICATION ENTRY POINT (MAIN BOOT)
# =============================================================================

def main():
    """
    Main execution lifecycle. Inherits PWD from Siril, enforces user home directory
    barriers against parasite disk walking, and runs the Qt6 native event loop.
    """
    app = QApplication(sys.argv)

    base_search_dir = os.getcwd()
    user_home = os.path.expanduser("~")

    # Anti-parasite protection: block execution if Siril PWD matches system user home
    if os.path.abspath(base_search_dir) == os.path.abspath(user_home):
        detected_paths = []
    else:
        detected_paths = scan_for_lights_directories(base_search_dir)

    cache_directory = os.path.join(base_search_dir, "astroview_cache")

    last_known_state = {}
    state_file = os.path.join(cache_directory, "cache_state.json")
    if os.path.exists(state_file) and detected_paths:
        try:
            with open(state_file, "r") as f:
                last_known_state = json.load(f)
        except:
            pass

    gui = FITSImagePlayerGui(detected_paths, cache_directory, last_known_state)
    gui.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
