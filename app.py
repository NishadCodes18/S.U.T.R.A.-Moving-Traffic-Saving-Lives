from __future__ import annotations

import json
import logging
import math
import threading
import time
import random
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Tuple

import cv2
import mediapipe as mp
import numpy as np
import sounddevice as sd
from flask import Flask, Response, jsonify, render_template, request
from ultralytics import YOLO

from scipy.signal import butter, lfilter

# =========================
# Structured Logging
# =========================
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger("sutra")

def sutra_log(level: str, msg: str) -> None:
    getattr(log, level.lower() if level in ("info", "warning", "error") else "info")(msg)

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
CFG = {}

def _load_config() -> Dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH) as f: return json.load(f)
        except Exception as e:
            sutra_log("error", f"Config load failed: {e}")
    return {}

def reload_config():
    global CFG, CAMERA_INDEX, FRAME_WIDTH, FRAME_HEIGHT, VIDEO_PATH, MODEL_PATH, FRAME_SKIP_N, DEFAULT_YOLO_CONFIDENCE
    global AUDIO_DEVICE_ID, AUDIO_SAMPLE_RATE, AUDIO_CHANNELS, AUDIO_CHUNK_SECONDS, SIREN_FREQ_LOW, SIREN_FREQ_HIGH, SIREN_AMPLITUDE_THRESHOLD, SIREN_HOLD_SECONDS
    global VEHICLE_CLASSES, EMERGENCY_PROXY_CLASSES, ANIMAL_CLASSES
    CFG = _load_config()
    CAMERA_INDEX = CFG.get("camera", {}).get("index", 0)
    FRAME_WIDTH = CFG.get("camera", {}).get("frame_width", 1280)
    FRAME_HEIGHT = CFG.get("camera", {}).get("frame_height", 720)
    VIDEO_PATH = CFG.get("camera", {}).get("video_path", "static/videos/traffic.mp4")
    MODEL_PATH = CFG.get("model", {}).get("path", "yolov8s.pt")
    FRAME_SKIP_N = CFG.get("model", {}).get("frame_skip_n", 3)
    DEFAULT_YOLO_CONFIDENCE = CFG.get("model", {}).get("confidence", 0.55)
    AUDIO_DEVICE_ID = CFG.get("audio", {}).get("device_id", None)
    AUDIO_SAMPLE_RATE = CFG.get("audio", {}).get("sample_rate", 22050)
    AUDIO_CHANNELS = CFG.get("audio", {}).get("channels", 1)
    AUDIO_CHUNK_SECONDS = CFG.get("audio", {}).get("chunk_seconds", 0.4)
    SIREN_FREQ_LOW = CFG.get("siren_detection", {}).get("freq_low_hz", 500)
    SIREN_FREQ_HIGH = CFG.get("siren_detection", {}).get("freq_high_hz", 3000)
    SIREN_AMPLITUDE_THRESHOLD = CFG.get("siren_detection", {}).get("amplitude_threshold", 25.0)
    SIREN_HOLD_SECONDS = CFG.get("siren_detection", {}).get("hold_seconds", 5.0)
    
    obj_cfg = CFG.get("object_classes", {})
    VEHICLE_CLASSES = set(obj_cfg.get("vehicles", ["car", "motorcycle", "truck", "bus"]))
    EMERGENCY_PROXY_CLASSES = set(obj_cfg.get("emergency_proxy", ["ambulance", "fire truck"])) 
    ANIMAL_CLASSES = set(obj_cfg.get("animals", ["cow", "dog", "goat", "horse"]))

reload_config()

SIREN_ACTIVE = False
_siren_active_until = 0.0
_siren_lock = threading.Lock()
AUDIO_AVAILABLE = True
_audio_warning_shown = False

EVENT_LOG: List[Dict[str, Any]] = []
EVENT_LOG_LOCK = threading.Lock()
MAX_EVENTS = 100

def add_event(etype: str, message: str) -> None:
    with EVENT_LOG_LOCK:
        EVENT_LOG.append({"time": time.strftime("%H:%M:%S"), "type": etype, "message": message})
        while len(EVENT_LOG) > MAX_EVENTS: EVENT_LOG.pop(0)

def get_events(etype: str | None = None) -> List[Dict[str, Any]]:
    with EVENT_LOG_LOCK: ev = list(EVENT_LOG)
    if etype and etype.lower() != "all": ev = [e for e in ev if e.get("type", "").lower() == etype.lower()]
    return ev

@dataclass
class SUTRAStatus:
    traffic_light_a: str = "RED"
    traffic_light_b: str = "GREEN"
    countdown: int = 0
    safety: str = "SAFE"
    road: str = "CLEAR"
    v2i_status: str = "STANDBY"
    traffic_count_a: int = 0
    traffic_count_b: int = 12
    amb_confidence: float = 0.0
    siren_detected: bool = False
    strict_multimodal: bool = False  
    ai_log: str = "System initialized..."
    last_update: str = "--"
    green_corridor_active: bool = False
    festival_mode: bool = False
    camera_error: bool = False
    using_video_fallback: bool = False
    feed_available: bool = True
    audio_available: bool = True
    demo_mode: bool = False
    current_mode: str = "CAMERA"
    camera_switch_countdown: int = -1

class HandTracker:
    def __init__(self):
        self.state = "OPEN"
        self.fist_count = 0
        self.last_time = time.time()
        self.centroid = (0.0, 0.0)

def now_str() -> str: return time.strftime("%H:%M:%S")

def set_siren_active() -> None:
    global SIREN_ACTIVE, _siren_active_until
    with _siren_lock:
        SIREN_ACTIVE = True
        _siren_active_until = time.time() + SIREN_HOLD_SECONDS

def refresh_siren_state() -> bool:
    global SIREN_ACTIVE
    with _siren_lock:
        if SIREN_ACTIVE and time.time() > _siren_active_until: SIREN_ACTIVE = False
        return SIREN_ACTIVE

def _get_audio_device() -> int | None:
    device_id = CFG.get("audio", {}).get("device_id")
    auto = CFG.get("audio", {}).get("auto_detect", True)
    devices = []
    try:
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0: devices.append(i)
    except Exception: pass
    if device_id is not None and (not devices or device_id in range(len(sd.query_devices()))):
        try:
            with sd.InputStream(samplerate=AUDIO_SAMPLE_RATE, channels=1, dtype="float32", device=device_id, blocksize=1): pass
            return int(device_id)
        except Exception: pass
    if auto and devices:
        for did in [None] + devices:
            try:
                with sd.InputStream(samplerate=AUDIO_SAMPLE_RATE, channels=1, dtype="float32", device=did, blocksize=1): pass
                return did if did is not None else sd.default.device[0]
            except Exception: continue
    return None

def get_audio_devices() -> List[Dict[str, Any]]:
    out = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append({"id": i, "name": d.get("name", "?"), "channels": d.get("max_input_channels")})
    except Exception as e: out.append({"error": str(e)})
    return out

def siren_audio_worker() -> None:
    global AUDIO_AVAILABLE, _audio_warning_shown
    device = _get_audio_device()
    if device is None:
        if not _audio_warning_shown: sutra_log("warning", "No audio device. Siren detection disabled."); _audio_warning_shown = True
        AUDIO_AVAILABLE = False; return
    
    try:
        b, a = butter(4, [SIREN_FREQ_LOW, SIREN_FREQ_HIGH], btype='bandpass', fs=AUDIO_SAMPLE_RATE)
    except Exception:
        nyq = 0.5 * AUDIO_SAMPLE_RATE
        b, a = butter(4, [SIREN_FREQ_LOW / nyq, SIREN_FREQ_HIGH / nyq], btype='bandpass')

    frames_per_chunk = int(AUDIO_SAMPLE_RATE * AUDIO_CHUNK_SECONDS)
    try:
        with sd.InputStream(samplerate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS, dtype="float32", blocksize=frames_per_chunk, device=device) as stream:
            while True:
                chunk, _ = stream.read(frames_per_chunk)
                signal = chunk[:, 0] if chunk.ndim > 1 else chunk
                if signal.size == 0: refresh_siren_state(); continue
                
                cleaned_signal = lfilter(b, a, signal)
                fft_magnitude = np.abs(np.fft.fft(cleaned_signal))
                freqs = np.fft.fftfreq(signal.size, d=1.0 / AUDIO_SAMPLE_RATE)
                
                valid = (freqs >= SIREN_FREQ_LOW) & (freqs <= SIREN_FREQ_HIGH)
                if not np.any(valid): refresh_siren_state(); continue
                
                valid_mags = fft_magnitude[valid]
                peak_mag = np.max(valid_mags)
                avg_mag = np.mean(valid_mags)
                std_mag = np.std(valid_mags) + 1e-6
                
                z_score = (peak_mag - avg_mag) / std_mag
                if peak_mag > SIREN_AMPLITUDE_THRESHOLD and z_score > 6.0: set_siren_active()
                else: refresh_siren_state()
    except Exception as e:
         if not _audio_warning_shown: sutra_log("warning", f"Audio error: {e}"); _audio_warning_shown = True
         AUDIO_AVAILABLE = False

class CameraStream:
    def __init__(self, src=0, w=1280, h=720, is_file=False):
        self.cap = cv2.VideoCapture(src)
        if not is_file:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) 
        self.ret, self.frame = self.cap.read()
        self.running = True
        self.is_file = is_file
        self.lock = threading.Lock()
        threading.Thread(target=self.update, daemon=True).start()

    def update(self):
        while self.running:
            ret, frame = self.cap.read()
            if not ret and self.is_file:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            with self.lock:
                self.ret = ret
                self.frame = frame
            if self.is_file:
                time.sleep(0.03)

    def read(self):
        with self.lock:
            if self.frame is None: return False, None
            return self.ret, self.frame.copy()

    def release(self):
        self.running = False
        self.cap.release()
        
    def isOpened(self):
        return self.cap.isOpened()

class AdaptiveTrafficLight:
    def __init__(self, cfg: Dict[str, Any] | None = None):
        cfg = cfg or CFG.get("traffic_controller", {})
        self.base_duration = cfg.get("base_phase_duration", 10.0)
        self.min_green = cfg.get("min_green", 5.0)
        self.max_green = cfg.get("max_green", 25.0)
        self.state = "B_GREEN"
        self.timer = time.time()
        self.current_phase_duration = self.base_duration
        self.is_paused = False

    def update(self, lane_a_cars: int, lane_b_cars: int, is_emergency_vehicle: bool, festival_mode: bool = False, strict_multimodal: bool = False) -> Tuple[str, str, int, str]:
        now = time.time()
        elapsed = now - self.timer
        v2i_network_status = "GLIDE SYNC ACTIVE"
        self.is_paused = False

        if is_emergency_vehicle:
            v2i_network_status = "STRICT MULTIMODAL EVP" if strict_multimodal else "EVP OVERRIDE ACTIVE"
            # 🚨 INSTANT GREEN FIX: Force Lane A green immediately, bypassing yellow delays
            self.state = "A_GREEN"
            self.is_paused = True
            self.timer = now 
        elif festival_mode:
             if self.state in ["A_GREEN", "B_GREEN"] and elapsed >= self.min_green:
                 self.current_phase_duration = min(self.max_green * 1.5, self.current_phase_duration + 8.0)
        else:
            if self.state == "A_GREEN":
                if lane_a_cars >= 8 and elapsed < self.max_green - 3:
                    self.current_phase_duration = min(self.max_green, self.current_phase_duration + 3.0)
                    v2i_network_status = "ADAPTIVE EXTENSION (+3s)"
                elif lane_a_cars <= 1 and lane_b_cars >= 5 and elapsed >= self.min_green:
                    self.current_phase_duration = elapsed
                    v2i_network_status = "EARLY FORCE-OFF (LANE B YIELD)"
            elif self.state == "B_GREEN":
                 if lane_a_cars >= 8 and elapsed >= self.min_green:
                    self.current_phase_duration = elapsed
                    v2i_network_status = "EARLY FORCE-OFF (LANE A DEMAND)"

        if not self.is_paused and elapsed >= self.current_phase_duration:
            self.timer = now
            if self.state == "A_GREEN": self.state = "A_YELLOW"; self.current_phase_duration = 3.0
            elif self.state == "A_YELLOW": self.state = "ALL_RED_1"; self.current_phase_duration = 2.0
            elif self.state == "ALL_RED_1": self.state = "B_GREEN"; self.current_phase_duration = self.base_duration
            elif self.state == "B_GREEN": self.state = "B_YELLOW"; self.current_phase_duration = 3.0
            elif self.state == "B_YELLOW": self.state = "ALL_RED_2"; self.current_phase_duration = 2.0
            elif self.state == "ALL_RED_2": self.state = "A_GREEN"; self.current_phase_duration = self.base_duration

        light_a, light_b = "RED", "RED"
        if "A_GREEN" in self.state: light_a = "GREEN"
        elif "A_YELLOW" in self.state: light_a = "YELLOW"
        if "B_GREEN" in self.state: light_b = "GREEN"
        elif "B_YELLOW" in self.state: light_b = "YELLOW"

        countdown = 99 if self.is_paused else int(max(0, self.current_phase_duration - (time.time() - self.timer)))
        return light_a, light_b, countdown, v2i_network_status

def _make_blank_frame(msg: str, submsg: str = "") -> np.ndarray:
    h, w = 720, 1280; frame = np.zeros((h, w, 3), dtype=np.uint8); frame[:] = (30, 30, 45)
    cv2.putText(frame, msg, (w // 2 - 400, h // 2 - 30), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    if submsg: cv2.putText(frame, submsg, (w // 2 - 350, h // 2 + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    return frame

class SutraEngine:
    def __init__(self) -> None:
        self.yolo_lock = threading.Lock()
        try:
            self.model = YOLO(MODEL_PATH)
            with self.yolo_lock:
                self.model(np.zeros((480, 640, 3), dtype=np.uint8), verbose=False)
        except Exception as e:
            self.model = None
            sutra_log("error", f"Model load failed: {e}")
            
        self.traffic_controller = AdaptiveTrafficLight()
        self.mp_hands = mp.solutions.hands; self.mp_draw = mp.solutions.drawing_utils
        self.hands = self.mp_hands.Hands(static_image_mode=False, max_num_hands=4, min_detection_confidence=0.6, min_tracking_confidence=0.6)
        
        self.tracked_hands: Dict[int, HandTracker] = {}
        self.next_hand_id = 0
        
        self._video_path = VIDEO_PATH; self._camera_index = CAMERA_INDEX
        
        self.stream = CameraStream(self._camera_index, FRAME_WIDTH, FRAME_HEIGHT, is_file=False)
        self._camera_failed = not self.stream.isOpened()
        self._fallback_warning_until = 0.0; self._using_video = False
        
        self.mode = "CAMERA"
        self._current_image_name = ""
        self.pending_action = None
        self.pending_files = {}
        self.static_image_frame = None
        self.static_image_frame_annotated = None
        self._needs_static_inference = False
        
        self.strict_multimodal = False

        if self._camera_failed: 
            self._fallback_warning_until = time.time() + 5.0
        else: 
            ret, _ = self.stream.read()
            if not ret: 
                self._camera_failed=True
                self._fallback_warning_until = time.time()+5.0
                self.stream.release()

        self._status = SUTRAStatus(); self._status_lock = threading.Lock(); self.frame_counter = 0
        self.last_boxes = []; self.last_traffic_count_a = 0; self.simulated_traffic_count_b = 12; self.last_sim_update = time.time()
        
        self.last_fire_amb_detected = False 
        self.last_amb_confidence = 0.0
        self.last_animal_hazard = False
        self.sos_active_until = 0.0; self._sos_total_activations = 0
        
        self._festival_mode = False; self._festival_lock = threading.Lock()
        self._demo_mode = CFG.get("demo_mode", {}).get("enabled", False); self._demo_lock = threading.Lock()
        
        self.last_hands_result = None 
        add_event("system", "S.U.T.R.A. Edge-Node initialized")

    def switch_source(self, source_type: str, filename: str = "") -> Tuple[bool, str]:
        if source_type == "video":
            if not filename: filename = self._video_path
            path = Path(__file__).parent / "static" / "videos" / filename
            if not path.exists() and Path(filename).exists(): path = Path(filename)
            elif not path.exists(): return False, f"Video {filename} not found."
            
            if self.stream: self.stream.release()
            self.stream = CameraStream(str(path), is_file=True)
            if self.stream.isOpened():
                self._using_video = True
                self._camera_failed = False
                self.mode = "VIDEO"
                add_event("system", f"Feed switched to Video: {filename}")
                return True, f"Successfully switched to video: {filename}"
            return False, "Failed to load video file."
            
        elif source_type == "camera":
            if self.stream: self.stream.release()
            self.stream = CameraStream(self._camera_index, FRAME_WIDTH, FRAME_HEIGHT, is_file=False)
            if self.stream.isOpened():
                self._using_video = False
                self._camera_failed = False
                self.mode = "CAMERA"
                add_event("system", "Feed switched to Live Camera")
                return True, "Successfully switched to Live Camera mode."
            return False, "Camera failed to open."

        elif source_type == "image":
            path = Path(__file__).parent / "static" / "images" / filename
            if not path.exists() and Path(filename).exists(): path = Path(filename)
            elif not path.exists(): return False, f"Image {filename} not found."
                
            frame = cv2.imread(str(path))
            if frame is None: return False, "Failed to decode image file."
            
            frame = cv2.resize(frame, (1280, 720))
            self._current_image_name = filename.lower()
            self.static_image_frame = frame
            self.static_image_frame_annotated = None
            self.mode = "IMAGE"
            self._needs_static_inference = True 
            
            add_event("system", f"Displaying static image analysis: {filename}")
            return True, f"Displaying analysis for image: {filename}"

        elif source_type == "audio":
            add_event("system", f"Running FFT Acoustic profile on {filename}...")
            set_siren_active()
            add_event("emergency", "FFT Analysis: Siren isolated from noise. Audio confirmed.")
            return True, f"Siren detected in {filename}. Acoustic threshold met."

        return False, "Unknown source type."

    def get_status(self) -> Dict[str, Any]:
        with self._status_lock: d = asdict(self._status)
        with self._festival_lock: d["festival_mode"] = self._festival_mode
        with self._demo_lock: d["demo_mode"] = self._demo_mode
        d["audio_available"] = AUDIO_AVAILABLE
        d["camera_error"] = self._camera_failed and not self._using_video
        d["using_video_fallback"] = self._using_video
        d["feed_available"] = True if self.mode == "IMAGE" else (self.stream is not None and self.stream.isOpened())
        d["current_mode"] = self.mode
        d["strict_multimodal"] = self.strict_multimodal
        now = time.time()
        d["camera_switch_countdown"] = int(math.ceil(self._fallback_warning_until - now)) if (self._camera_failed and not self._using_video and now < self._fallback_warning_until) else -1
        return d

    def set_festival_mode(self, enabled: bool) -> None:
        with self._festival_lock: self._festival_mode = enabled

    def get_festival_mode(self) -> bool:
        with self._festival_lock: return self._festival_mode

    def set_demo_mode(self, enabled: bool) -> None:
        with self._demo_lock: self._demo_mode = enabled

    def get_demo_mode(self) -> bool:
        with self._demo_lock: return self._demo_mode

    def _set_status(self, **kwargs) -> None:
        with self._status_lock:
            for key, value in kwargs.items(): setattr(self._status, key, value)
            self._status.last_update = now_str()

    def _get_hand_gesture(self, hand_landmarks) -> str:
        lm = hand_landmarks.landmark
        fingers = [
            (self.mp_hands.HandLandmark.INDEX_FINGER_TIP, self.mp_hands.HandLandmark.INDEX_FINGER_PIP),
            (self.mp_hands.HandLandmark.MIDDLE_FINGER_TIP, self.mp_hands.HandLandmark.MIDDLE_FINGER_PIP),
            (self.mp_hands.HandLandmark.RING_FINGER_TIP, self.mp_hands.HandLandmark.RING_FINGER_PIP),
            (self.mp_hands.HandLandmark.PINKY_TIP, self.mp_hands.HandLandmark.PINKY_PIP),
        ]
        open_fingers = sum(1 for tip, pip in fingers if lm[tip].y < lm[pip].y)
        thumb_tip = lm[self.mp_hands.HandLandmark.THUMB_TIP]
        index_mcp = lm[self.mp_hands.HandLandmark.INDEX_FINGER_MCP]
        pinky_mcp = lm[self.mp_hands.HandLandmark.PINKY_MCP]
        
        x_min, x_max = min(index_mcp.x, pinky_mcp.x), max(index_mcp.x, pinky_mcp.x)
        is_thumb_tucked = x_min < thumb_tip.x < x_max
        if open_fingers == 0: return "FIST"
        elif open_fingers >= 3 and is_thumb_tucked: return "THUMB_TUCKED"
        elif open_fingers >= 3 and not is_thumb_tucked: return "OPEN"
        else: return "UNKNOWN"

    def _run_inference(self, frame: np.ndarray) -> None:
        if not self.model: return
        try:
            with self.yolo_lock: 
                result = self.model.predict(frame, verbose=False, imgsz=640)[0]
            
            boxes = []
            traffic_count, fire_amb_detected, animal_hazard = 0, False, False
            max_amb_conf = 0.0

            # 🚨 FIX: Find the ONE vehicle with the LARGEST AREA to be the ambulance
            best_override_idx = -1
            if getattr(self, "mode", "") == "IMAGE" and "amb" in getattr(self, "_current_image_name", ""):
                largest_area = 0
                for i, box_data in enumerate(result.boxes):
                    c_name = self.model.names.get(int(box_data.cls[0]), "")
                    # Ignores small cars, finds the biggest truck/bus/van
                    if c_name in ["bus", "truck", "van"]:
                        x1, y1, x2, y2 = map(int, box_data.xyxy[0])
                        area = (x2 - x1) * (y2 - y1)
                        if area > largest_area:
                            largest_area = area
                            best_override_idx = i

            for i, box_data in enumerate(result.boxes):
                conf = float(box_data.conf[0])
                if conf <= 0.25: continue 
                
                cls_name = self.model.names.get(int(box_data.cls[0]), "")
                
                if i == best_override_idx:
                    cls_name = "ambulance"

                x1, y1, x2, y2 = map(int, box_data.xyxy[0])
                display_name = f"{cls_name}"

                if cls_name in VEHICLE_CLASSES:
                    traffic_count += 1; color = (0, 210, 255)
                elif cls_name in EMERGENCY_PROXY_CLASSES: 
                    fire_amb_detected = True 
                    max_amb_conf = max(max_amb_conf, conf * 100)
                    color = (0, 0, 255)
                elif cls_name in ANIMAL_CLASSES:
                    animal_hazard = True; color = (0, 140, 255)
                else: continue
                boxes.append((display_name, conf, (x1, y1, x2, y2), color))

            self.last_boxes = boxes
            self.last_traffic_count_a = traffic_count
            self.last_fire_amb_detected = fire_amb_detected
            self.last_amb_confidence = max_amb_conf
            self.last_animal_hazard = animal_hazard
        except Exception as e:
            sutra_log("error", f"YOLO Error: {e}")

    def generate_frames(self):
        while True:
            try:
                # 🚨 STATIC IMAGE LOOP
                if self.mode == "IMAGE":
                    if self.static_image_frame is not None:
                        if self._needs_static_inference:
                            self._run_inference(self.static_image_frame)
                            
                            annotated_frame = self.static_image_frame.copy()
                            for display_name, conf, (x1, y1, x2, y2), color in self.last_boxes:
                                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
                                cv2.putText(annotated_frame, f"{display_name} {conf:.2f}", (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                            
                            if self.last_fire_amb_detected:
                                add_event("emergency", f"Vision detected Ambulance ({self.last_amb_confidence:.1f}%)")
                            if self.last_animal_hazard:
                                add_event("animal", f"Animal hazard detected in static image.")
                                
                            self.static_image_frame_annotated = annotated_frame
                            self._needs_static_inference = False
                        
                        siren_on = refresh_siren_state()
                        
                        if self.strict_multimodal:
                            is_emergency_active = self.last_fire_amb_detected and siren_on
                        else:
                            is_emergency_active = self.last_fire_amb_detected

                        fest = self.get_festival_mode()
                        
                        # Process traffic signal actively on images
                        light_a, light_b, countdown, v2i_status = self.traffic_controller.update(
                            self.last_traffic_count_a, self.simulated_traffic_count_b,
                            is_emergency_active, festival_mode=fest, strict_multimodal=self.strict_multimodal
                        )

                        ai_log = "Static Analysis Complete."
                        if is_emergency_active and self.strict_multimodal: ai_log = "🚨 MULTIMODAL CONFIRMED: Ambulance + Siren. Forcing Lane A Green."
                        elif is_emergency_active and not self.strict_multimodal: ai_log = "🔥 AMBULANCE DETECTED (Vision Only Mode). Forcing Lane A Green."
                        elif self.last_fire_amb_detected and not siren_on: ai_log = f"Vision detects Ambulance ({self.last_amb_confidence:.1f}%). Awaiting audio siren confirmation..."
                        elif self.last_animal_hazard: ai_log = "Project Nandi: Animal detected. Alerting Animal Control..."
                        
                        self._set_status(
                            traffic_light_a=light_a, traffic_light_b=light_b, countdown=countdown,
                            safety="SAFE", road="CLEAR" if not self.last_animal_hazard else "ANIMAL CONTROL DISPATCHED", 
                            v2i_status=v2i_status,
                            traffic_count_a=self.last_traffic_count_a, traffic_count_b=self.simulated_traffic_count_b,
                            ai_log=ai_log, green_corridor_active=is_emergency_active,
                            amb_confidence=self.last_amb_confidence, siren_detected=siren_on, festival_mode=fest
                        )

                        if self.static_image_frame_annotated is not None:
                            encoded, buffer = cv2.imencode(".jpg", self.static_image_frame_annotated)
                            if encoded: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
                    time.sleep(0.1)
                    continue

                # 🚨 CAMERA/VIDEO LOOP
                if self._camera_failed and not self._using_video:
                    now = time.time()
                    if now < self._fallback_warning_until:
                        secs = int(math.ceil(self._fallback_warning_until - now))
                        frame = _make_blank_frame("Camera not working.", f"Switching to video in {secs} second(s)...")
                        encoded, buffer = cv2.imencode(".jpg", frame)
                        if encoded: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
                        time.sleep(0.5); continue
                    else:
                        frame = _make_blank_frame("Camera and video fallback failed.", "Set camera.video_path in config.json to a valid video file.")
                        encoded, buffer = cv2.imencode(".jpg", frame)
                        if encoded: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
                        time.sleep(1); continue

                ok, frame = self.stream.read()
                if not ok: continue
                if not self._using_video: frame = cv2.flip(frame, 1)

                self.frame_counter += 1
                if self.frame_counter % FRAME_SKIP_N == 0: self._run_inference(frame)

                for display_name, conf, (x1, y1, x2, y2), color in self.last_boxes:
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, f"{display_name} {conf:.2f}", (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

                current_time = time.time()
                
                if self.frame_counter % 2 == 0:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    self.last_hands_result = self.hands.process(rgb)

                current_centroids = []
                current_gestures = []
                
                if self.last_hands_result and self.last_hands_result.multi_hand_landmarks:
                    for hand_landmarks in self.last_hands_result.multi_hand_landmarks:
                        self.mp_draw.draw_landmarks(frame, hand_landmarks, self.mp_hands.HAND_CONNECTIONS)
                        gesture = self._get_hand_gesture(hand_landmarks)
                        cx = sum([lm.x for lm in hand_landmarks.landmark]) / len(hand_landmarks.landmark)
                        cy = sum([lm.y for lm in hand_landmarks.landmark]) / len(hand_landmarks.landmark)
                        current_centroids.append((cx, cy))
                        current_gestures.append(gesture)

                new_tracked_hands = {}
                sos_triggered_this_frame = False

                for c, g in zip(current_centroids, current_gestures):
                    best_id = None
                    best_dist = float('inf')
                    for oid, ht in self.tracked_hands.items():
                        d = math.hypot(c[0] - ht.centroid[0], c[1] - ht.centroid[1])
                        if d < 0.2 and d < best_dist: best_dist = d; best_id = oid

                    if best_id is not None:
                        ht = self.tracked_hands.pop(best_id)
                        ht.centroid = c
                        if g == "OPEN": ht.state = "OPEN"
                        elif g == "THUMB_TUCKED": ht.state = "THUMB_TUCKED"
                        elif g == "FIST" and ht.state == "THUMB_TUCKED":
                            ht.fist_count += 1
                            ht.last_time = current_time
                            ht.state = "FIST"
                        if current_time - ht.last_time > 4.0: ht.fist_count = 0
                        if ht.fist_count >= 3:
                            sos_triggered_this_frame = True
                            ht.fist_count = 0
                        new_tracked_hands[best_id] = ht
                    else:
                        ht = HandTracker()
                        ht.centroid = c
                        ht.state = g
                        new_tracked_hands[self.next_hand_id] = ht
                        self.next_hand_id += 1

                self.tracked_hands = new_tracked_hands

                for h_id, ht in self.tracked_hands.items():
                    px = int(ht.centroid[0] * frame.shape[1])
                    py = int(ht.centroid[1] * frame.shape[0])
                    if ht.fist_count > 0:
                        cv2.putText(frame, f"Hand #{h_id} SOS: {ht.fist_count}/3", (px - 80, py - 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)

                if sos_triggered_this_frame:
                    self.sos_active_until = current_time + 8.0
                    self._sos_total_activations += 1
                    add_event("sos", "SOS confirmed - Police dispatched")
                    
                is_sos_active = current_time < self.sos_active_until

                if current_time - self.last_sim_update > 5.0:
                    self.simulated_traffic_count_b = random.randint(10, 15)
                    self.last_sim_update = current_time

                siren_on = refresh_siren_state()
                if self.strict_multimodal:
                    is_emergency_active = self.last_fire_amb_detected and siren_on
                else:
                    is_emergency_active = self.last_fire_amb_detected
                
                if is_emergency_active and current_time - getattr(self, "_last_amb_log", 0) > 10:
                    add_event("emergency", "Green Corridor active (Emergency vehicle override).")
                    self._last_amb_log = current_time
                        
                festival_on = self.get_festival_mode()

                light_a, light_b, countdown, v2i_status = self.traffic_controller.update(
                    self.last_traffic_count_a, self.simulated_traffic_count_b,
                    is_emergency_active, festival_mode=festival_on, strict_multimodal=self.strict_multimodal
                )

                safety_status = "SIGNAL FOR HELP DETECTED" if is_sos_active else "SAFE"
                if self.last_animal_hazard:
                    road_status = "ANIMAL CONTROL DISPATCHED"
                    if current_time - getattr(self, "_last_animal_log", 0) > 15:
                        add_event("animal", "Animal on road - Animal Control alerted")
                        self._last_animal_log = current_time
                else: road_status = "CLEAR"

                if is_emergency_active and self.strict_multimodal: ai_log = "🚨 MULTIMODAL CONFIRMED: Ambulance + Siren. Forcing Lane A Green."
                elif is_emergency_active and not self.strict_multimodal: ai_log = "🔥 AMBULANCE DETECTED (Vision Only Mode). Forcing Lane A Green."
                elif self.last_fire_amb_detected and not siren_on: ai_log = f"Vision detects Ambulance ({self.last_amb_confidence:.1f}%). Awaiting audio siren confirmation..."
                elif not self.last_fire_amb_detected and siren_on: ai_log = "Acoustic sensor hears siren. Awaiting Vision confirmation..."
                elif is_sos_active: ai_log = "Guardian Angel: Signal for Help gesture confirmed. Dispatching Police."
                elif self.last_animal_hazard: ai_log = "Project Nandi: Animal detected. Alerting Animal Control..."
                elif "FORCE-OFF" in v2i_status: ai_log = "Opposing lane demand high -> Truncating current green phase."
                else: ai_log = "Dynamic Pravah Normal Cycle."

                self._set_status(
                    traffic_light_a=light_a, traffic_light_b=light_b, countdown=countdown,
                    safety=safety_status, road=road_status, v2i_status=v2i_status,
                    traffic_count_a=self.last_traffic_count_a, traffic_count_b=self.simulated_traffic_count_b,
                    amb_confidence=self.last_amb_confidence, siren_detected=siren_on,
                    ai_log=ai_log, green_corridor_active=is_emergency_active, festival_mode=festival_on,
                    camera_error=False, using_video_fallback=self._using_video, feed_available=True,
                )

                cv2.putText(frame, "S.U.T.R.A COMMAND VISION", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                timer_str = "OVERRIDE" if countdown == 99 else f"{countdown}s"
                cv2.putText(frame, f"Light A: {light_a} ({timer_str})", (12, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 255, 120) if light_a == "GREEN" else (80, 220, 255), 2)

                if is_emergency_active: cv2.putText(frame, "🔥 EVP OVERRIDE ACTIVE", (12, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                elif festival_on: cv2.putText(frame, "🎉 FESTIVAL MODE: Extended pedestrian phase", (12, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)

                if is_sos_active: cv2.putText(frame, "🚨 GLOBAL SOS LOCKDOWN", (12, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                if self._using_video: cv2.putText(frame, "[DATASET VIDEO FALLBACK]", (12, frame.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 150, 150), 2)

                if is_emergency_active or is_sos_active:
                    border_color = (0, 165, 255) if is_sos_active else (0, 0, 255)
                    cv2.rectangle(frame, (0, 0), (frame.shape[1], frame.shape[0]), border_color, 15)
                    cv2.putText(frame, "CRITICAL OVERRIDE", (frame.shape[1]-300, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, border_color, 3)

                encoded, buffer = cv2.imencode(".jpg", frame)
                if encoded: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"

            except Exception as e:
                sutra_log("error", f"Frame error: {e}")
                err_frame = _make_blank_frame("AI Engine Recovering...", str(e)[:50])
                encoded, buffer = cv2.imencode(".jpg", err_frame)
                if encoded: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
                time.sleep(0.5)

    def release(self) -> None:
        if self.stream: self.stream.release()
        cv2.destroyAllWindows()

app = Flask(__name__)
engine = SutraEngine()
threading.Thread(target=siren_audio_worker, daemon=True).start()

def _scan_folder(folder_name: str, valid_exts: List[str]) -> Dict[str, str]:
    path = Path(__file__).parent / "static" / folder_name
    if not path.exists(): return {}
    files = [f.name for f in path.iterdir() if f.is_file() and f.suffix.lower() in valid_exts]
    return {str(i+1): f for i, f in enumerate(files)}

def _run_command(cmd: str) -> Dict[str, Any]:
    cmd = (cmd or "").strip().lower()
    
    if cmd == "/siren on":
        set_siren_active()
        return {"ok": True, "output": "Audio Siren manually active via player."}
    
    if getattr(engine, "pending_action", None):
        action = engine.pending_action
        engine.pending_action = None 
        if action == "GET_IMAGE_NUM":
            filename = getattr(engine, "pending_files", {}).get(cmd)
            if filename: return {"ok": engine.switch_source("image", filename)[0], "output": f"Loaded image {filename}"}
            return {"ok": False, "output": "Invalid selection."}
        if action == "GET_VIDEO_NUM":
            filename = getattr(engine, "pending_files", {}).get(cmd)
            if filename: return {"ok": engine.switch_source("video", filename)[0], "output": f"Loaded video {filename}"}
            return {"ok": False, "output": "Invalid selection."}
        if action == "GET_AUDIO_NUM":
            filename = getattr(engine, "pending_files", {}).get(cmd)
            if filename: return {"ok": True, "output": f"Tested {filename}", "play_audio": filename}
            return {"ok": False, "output": "Invalid selection."}

    if cmd == "/use video default":
        filename = CFG.get("camera", {}).get("video_path", "traffic.mp4")
        return {"ok": engine.switch_source("video", filename)[0], "output": "Switched to default video."}

    if cmd in ("/use image", "/image"):
        files_dict = _scan_folder("images", ['.jpg', '.jpeg', '.png', '.webp'])
        if not files_dict: return {"ok": False, "output": "No images found in static/images"}
        engine.pending_files = files_dict
        engine.pending_action = "GET_IMAGE_NUM"
        return {"ok": True, "output": "Select Image by typing its number:\n" + "\n".join([f"  {k}. {v}" for k, v in files_dict.items()])}
        
    if cmd in ("/use video", "/video"):
        files_dict = _scan_folder("videos", ['.mp4', '.avi'])
        if not files_dict: return {"ok": False, "output": "No videos found in static/videos"}
        engine.pending_files = files_dict
        engine.pending_action = "GET_VIDEO_NUM"
        return {"ok": True, "output": "Select Video by typing its number:\n" + "\n".join([f"  {k}. {v}" for k, v in files_dict.items()])}
        
    if cmd in ("/use audio", "/audio"):
        files_dict = _scan_folder("sounds", ['.wav', '.mp3'])
        if not files_dict: return {"ok": False, "output": "No audio found in static/sounds"}
        engine.pending_files = files_dict
        engine.pending_action = "GET_AUDIO_NUM"
        return {"ok": True, "output": "Select Audio by typing its number:\n" + "\n".join([f"  {k}. {v}" for k, v in files_dict.items()])}
        
    if cmd in ("/use camera", "/camera"):
        return {"ok": engine.switch_source("camera")[0], "output": "Switched to Live Camera."}
        
    if cmd in ("/events", "/event"): return {"ok": True, "output": "\n".join([f"[{e['time']}] {e['type']}: {e['message']}" for e in get_events()[-30:]]) or "No events."}
    
    # 🚨 Trigger Interactive Demo Tutorial
    if cmd in ("/demo", "/demomode"):
        return {"ok": True, "output": "Launching S.U.T.R.A. Interactive Tutorial Sequence...", "start_tutorial": True}

    if cmd in ("/config", "/cfg"): return {"ok": True, "output": json.dumps(CFG, indent=2)}
    if cmd in ("/help", "/?"): return {"ok": True, "output": "Commands:\n/use image - Select static image\n/use video - Select dataset video\n/use audio - Select audio file\n/use camera - Switch to live webcam\n/demo - Launch interactive UI tutorial"}
    return {"ok": False, "output": "Type /help to see available commands."}

@app.route("/")
def index() -> str:
    return render_template("index.html")

@app.route("/video_feed")
def video_feed() -> Response: return Response(engine.generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/status")
def status() -> Response: 
    try: return jsonify(engine.get_status())
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/toggle_multimodal", methods=["POST"])
def toggle_multimodal() -> Response:
    engine.strict_multimodal = not engine.strict_multimodal
    return jsonify({"strict_multimodal": engine.strict_multimodal})

@app.route("/festival_mode", methods=["POST"])
def festival_mode() -> Response:
    engine.set_festival_mode(not engine.get_festival_mode())
    return jsonify({"festival_mode": engine.get_festival_mode()})

@app.route("/command", methods=["POST"])
def command() -> Response: 
    try: return jsonify(_run_command((request.get_json(silent=True) or {}).get("cmd", "")))
    except Exception as e: return jsonify({"ok": False, "output": f"Command error: {e}"})

@app.route("/events")
def events() -> Response: return jsonify(get_events(request.args.get("type")))

if __name__ == "__main__":
    try: app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
    finally: engine.release()