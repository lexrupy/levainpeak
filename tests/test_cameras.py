import pytest

from levainpeak.config import CameraConfig, load_camera_config, load_cameras, save_camera_config, save_cameras
from levainpeak.stream import A9Stream, RtspStream, UvcStream, open_stream

RTSP = CameraConfig(url="rtsp://192.168.0.10/s", name="Porta", model="Intelbras")
A9 = CameraConfig(name="A9", kind="a9", model="AUSEK AS-A8", ip="192.168.0.100", did="PPHWA9X", secret="$L8$x")
UVC = CameraConfig(name="Mesa", kind="uvc", device=2)


def test_several_cameras_round_trip_with_the_active_one(tmp_path):
    path = tmp_path / "camera.ini"
    save_cameras([RTSP, A9, UVC], "A9", path)
    cameras, active = load_cameras(path)
    assert cameras == [RTSP, A9, UVC] and active == "A9"
    assert load_camera_config(path) == A9


def test_the_older_single_section_still_reads_and_is_migrated(tmp_path):
    path = tmp_path / "camera.ini"
    path.write_text("[camera]\nurl = rtsp://h/x\nusername = u\npassword = p\ntransport = udp\n", encoding="utf-8")
    cameras, active = load_cameras(path)
    assert [c.name for c in cameras] == ["Câmera"] and cameras[0].transport == "udp"
    save_camera_config(A9, path)
    cameras, active = load_cameras(path)
    assert [c.name for c in cameras] == ["Câmera", "A9"] and active == "A9"
    assert "[camera]" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [
    CameraConfig(url="http://x", name="a"),
    CameraConfig(kind="a9", name="a", ip="1.2.3.4"),
    CameraConfig(kind="uvc", name="a", device=-1),
    CameraConfig(kind="ptz", name="a"),
    CameraConfig(url="rtsp://h/x", name=""),
])
def test_invalid_cameras_are_refused(bad, tmp_path):
    with pytest.raises(ValueError):
        save_cameras([bad], bad.name, tmp_path / "camera.ini")


def test_duplicate_names_are_refused(tmp_path):
    with pytest.raises(ValueError):
        save_cameras([RTSP, RTSP], "Porta", tmp_path / "camera.ini")


def test_the_stream_follows_the_kind():
    assert isinstance(open_stream(RTSP), RtspStream)
    assert isinstance(open_stream(A9), A9Stream)
    assert isinstance(open_stream(UVC), UvcStream)


def test_describe_never_carries_credentials():
    text = CameraConfig(url="rtsp://u:pw@10.0.0.1/s", password="pw", name="n").describe()
    assert "pw" not in text and "10.0.0.1" in text
    assert "$L8$x" not in A9.describe()


def test_a_truncated_jpeg_is_refused_not_half_grey():
    import cv2
    import numpy as np

    from levainpeak.stream import decode_whole_jpeg

    picture = np.random.default_rng(1).integers(0, 255, (120, 160, 3), dtype=np.uint8)
    jpeg = cv2.imencode(".jpg", picture)[1].tobytes()
    whole = decode_whole_jpeg(jpeg)
    assert whole is not None and whole.shape == (120, 160, 3)
    assert decode_whole_jpeg(jpeg[: len(jpeg) // 2]) is None
    assert decode_whole_jpeg(b"not a jpeg") is None
