"""The hardware layer: V4L2 ioctls, the gimbal, and the ffmpeg capture loop."""

import ctypes
import fcntl
import os
import struct
import subprocess
import threading
import time
from pathlib import Path


class V4L2Device:
    """Minimal stdlib ioctl access to a /dev/video* node. V4L2 allows a
    second fd for capability queries and controls while ffmpeg owns the
    stream, so this opens per call and never touches the video data."""

    QUERYCAP = 0x80685600
    QUERYCTRL = 0xC0445624
    S_CTRL = 0xC008561C
    S_EXT_CTRLS = 0xC0205648
    EXT_CONTROL_BYTES = 20
    CAP_VIDEO_CAPTURE = 0x00000001
    CAP_DEVICE_CAPS = 0x80000000
    CTRL_FLAG_DISABLED = 0x00000001

    def __init__(self, path: str):
        self.path = path

    def _ioctl(self, request: int, buffer: bytearray) -> bytearray:
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, request, buffer)
        finally:
            os.close(fd)
        return buffer

    def is_capture_device(self) -> bool:
        """False for the metadata sibling nodes modern UVC cameras expose
        (the Insta360 Link 2's /dev/video1 has no video formats at all)."""
        try:
            buffer = self._ioctl(self.QUERYCAP, bytearray(104))
        except OSError:
            return False
        capabilities, device_caps = struct.unpack_from("II", buffer, 84)
        effective = device_caps if capabilities & self.CAP_DEVICE_CAPS else capabilities
        return bool(effective & self.CAP_VIDEO_CAPTURE)

    def query_control(self, control_id: int) -> dict | None:
        buffer = bytearray(68)
        struct.pack_into("I", buffer, 0, control_id)
        try:
            self._ioctl(self.QUERYCTRL, buffer)
        except OSError:
            return None
        _, _, _, minimum, maximum, step, default, flags = struct.unpack_from("II32siiiiI", buffer)
        if flags & self.CTRL_FLAG_DISABLED:
            return None
        return {"min": minimum, "max": maximum, "step": step, "default": default}

    def set_control(self, control_id: int, value: int) -> None:
        self._ioctl(self.S_CTRL, bytearray(struct.pack("Ii", control_id, value)))

    def set_controls(self, values: dict[int, int]) -> None:
        """Sets several controls in one VIDIOC_S_EXT_CTRLS transaction.
        Needed for pan+tilt: they share one UVC control, the driver
        read-modify-writes single-axis changes, and cameras with broken
        readback (Insta360 Link 2) then fail validation with ERANGE."""
        array = ctypes.create_string_buffer(self.EXT_CONTROL_BYTES * len(values))
        for index, (control_id, value) in enumerate(values.items()):
            struct.pack_into("IIIi", array, self.EXT_CONTROL_BYTES * index, control_id, 0, 0, value)
        header = struct.pack("IIIiI4xQ", 0, len(values), 0, 0, 0, ctypes.addressof(array))
        self._ioctl(self.S_EXT_CTRLS, bytearray(header))


class GimbalPTZ:
    """Aims gimbal cameras (e.g. the Insta360 Link 2) through the standard
    UVC pan/tilt/zoom controls. Pan and tilt are absolute in arc-seconds;
    the Link 2 returns garbage when those controls are read back, so the
    last commanded pose is tracked here and the hardware is never asked."""

    PAN = 0x009A0908
    TILT = 0x009A0909
    ZOOM = 0x009A090D
    ARCSECONDS_PER_DEGREE = 3600

    def __init__(self, device: V4L2Device):
        self.device = device
        self.ranges = {
            "pan": device.query_control(self.PAN),
            "tilt": device.query_control(self.TILT),
            "zoom": device.query_control(self.ZOOM),
        }
        self.commanded: dict[str, float | int] = {}
        self.lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return any(self.ranges.values())

    def status(self) -> dict:
        axes = {}
        for name, control_range in self.ranges.items():
            if not control_range:
                continue
            if name in ("pan", "tilt"):
                axes[name] = {
                    "unit": "degrees",
                    "min": control_range["min"] / self.ARCSECONDS_PER_DEGREE,
                    "max": control_range["max"] / self.ARCSECONDS_PER_DEGREE,
                }
            else:
                axes[name] = {"unit": "native", **control_range}
        with self.lock:
            return {"supported": self.supported, "axes": axes, "commanded": dict(self.commanded) or None}

    def move(self, pan: float | None = None, tilt: float | None = None, zoom: int | None = None) -> dict:
        with self.lock:
            if pan is not None or tilt is not None:
                self._aim(pan, tilt)
            if zoom is not None:
                if not self.ranges["zoom"]:
                    raise ValueError("this camera has no zoom control")
                snapped = self._snap("zoom", zoom)
                self.device.set_control(self.ZOOM, snapped)
                self.commanded["zoom"] = snapped
        return self.status()

    def recenter(self) -> dict:
        zoom_default = self.ranges["zoom"]["default"] if self.ranges["zoom"] else None
        return self.move(
            pan=0 if self.ranges["pan"] else None,
            tilt=0 if self.ranges["tilt"] else None,
            zoom=zoom_default,
        )

    def _aim(self, pan: float | None, tilt: float | None) -> None:
        """Pan and tilt are one shared UVC control whose readback the
        Link 2 corrupts, so both axes are always written together, the
        unrequested one filled from the last commanded pose (0° when the
        pose was never commanded and is therefore unknowable)."""
        if not (self.ranges["pan"] and self.ranges["tilt"]):
            self._aim_single_axis(pan, tilt)
            return
        pan = self.commanded.get("pan", 0.0) if pan is None else pan
        tilt = self.commanded.get("tilt", 0.0) if tilt is None else tilt
        pan_units = self._snap("pan", pan * self.ARCSECONDS_PER_DEGREE)
        tilt_units = self._snap("tilt", tilt * self.ARCSECONDS_PER_DEGREE)
        self.device.set_controls({self.PAN: pan_units, self.TILT: tilt_units})
        self.commanded["pan"] = pan_units / self.ARCSECONDS_PER_DEGREE
        self.commanded["tilt"] = tilt_units / self.ARCSECONDS_PER_DEGREE

    def _aim_single_axis(self, pan: float | None, tilt: float | None) -> None:
        for name, control_id, degrees in (("pan", self.PAN, pan), ("tilt", self.TILT, tilt)):
            if degrees is None:
                continue
            if not self.ranges[name]:
                raise ValueError(f"this camera has no {name} control")
            units = self._snap(name, degrees * self.ARCSECONDS_PER_DEGREE)
            self.device.set_control(control_id, units)
            self.commanded[name] = units / self.ARCSECONDS_PER_DEGREE

    def _snap(self, name: str, value: float) -> int:
        control_range = self.ranges[name]
        step = control_range["step"] or 1
        clamped = max(control_range["min"], min(control_range["max"], value))
        return int(control_range["min"] + round((clamped - control_range["min"]) / step) * step)


class CameraFeed:
    """Owns the camera via a self-restarting ffmpeg that continuously
    overwrites the latest-frame JPEG.

    Configure via environment:
        EYES_DEVICE  camera node (default: newest capture-capable /dev/video*)
        EYES_ROTATE  0, 90, 180 or 270 degrees clockwise (default 0)
    """

    FPS = 4
    RESTART_DELAY_SECONDS = 3
    ROTATION_FILTERS = {"0": None, "90": "transpose=1", "180": "transpose=1,transpose=1", "270": "transpose=2"}

    def __init__(self, frame_path: Path):
        self.frame_path = frame_path
        self.device = os.environ.get("EYES_DEVICE") or self.newest_video_device()
        self.rotation_filter = self.ROTATION_FILTERS.get(os.environ.get("EYES_ROTATE", "0"))
        self.process: subprocess.Popen | None = None
        self.unpaused = threading.Event()
        self.unpaused.set()

    @staticmethod
    def newest_video_device() -> str:
        devices = sorted(Path("/dev").glob("video*"), key=lambda d: d.stat().st_mtime)
        capture_devices = [d for d in devices if V4L2Device(str(d)).is_capture_device()]
        if not capture_devices:
            raise SystemExit(
                "No capture-capable /dev/video* device found — plug in a webcam or set EYES_DEVICE."
            )
        return str(capture_devices[-1])

    def ffmpeg_command(self) -> list[str]:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-video_size", "1920x1080",
            "-i", self.device,
        ]
        if self.rotation_filter:
            command += ["-vf", self.rotation_filter]
        command += [
            "-r", str(self.FPS), "-q:v", "5",
            "-update", "1", "-y", str(self.frame_path),
        ]
        return command

    def run_forever(self) -> None:
        while True:
            self.unpaused.wait()
            self.process = subprocess.Popen(self.ffmpeg_command())
            self.process.wait()
            time.sleep(self.RESTART_DELAY_SECONDS)

    def frame_age_seconds(self) -> float | None:
        if not self.frame_path.exists():
            return None
        return time.time() - self.frame_path.stat().st_mtime

    def pause(self) -> None:
        self.unpaused.clear()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def resume(self) -> None:
        self.unpaused.set()

    def is_paused(self) -> bool:
        return not self.unpaused.is_set()
