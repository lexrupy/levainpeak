"""Local camera configuration."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


CAMERA_KINDS = ("rtsp", "a9", "uvc")
KIND_LABELS = {"rtsp": "RTSP (IP)", "a9": "A9 / Linklemo (LAN)", "uvc": "Webcam USB (UVC)"}
DEFAULT_CAMERA_NAME = "Câmera"
A9_DEFAULT_PORT = 20190


@dataclass(frozen=True)
class CameraConfig:
    url: str = ""
    username: str = ""
    password: str = ""
    transport: str = "tcp"
    read_timeout_seconds: int = 10
    name: str = DEFAULT_CAMERA_NAME
    kind: str = "rtsp"  # "rtsp", "a9" or "uvc"
    model: str = ""  # free text: which camera this is (e.g. "AUSEK AS-A8"); only informs
    ip: str = ""  # a9
    port: int = A9_DEFAULT_PORT  # a9
    did: str = ""  # a9: the device id
    secret: str = ""  # a9: paired_auth_secret
    device: int = 0  # uvc: index of /dev/video<N>

    def validate(self) -> None:
        if not self.name.strip() or any(c in self.name for c in "[]\n\r"):
            raise ValueError("A câmera precisa de um nome (sem colchetes).")
        if self.kind == "rtsp":
            self.stream_url()
        elif self.kind == "a9":
            if not (self.ip.strip() and self.did.strip() and self.secret.strip()):
                raise ValueError("A câmera A9 precisa de IP, DID e segredo.")
        elif self.kind == "uvc":
            if self.device < 0:
                raise ValueError("O índice da câmera UVC não pode ser negativo.")
        else:
            raise ValueError(f"Tipo de câmera desconhecido: {self.kind}")

    def describe(self) -> str:
        """A short line for the status bar; never carries the password or the secret."""
        where = {"rtsp": urlsplit(self.url).hostname or "", "a9": self.ip, "uvc": f"/dev/video{self.device}"}
        return " · ".join(part for part in (self.name, self.model, where.get(self.kind, "")) if part)

    def stream_url(self) -> str:
        raw_url = self.url.strip()
        parts = urlsplit(raw_url)
        if parts.scheme not in {"rtsp", "rtsps"} or not parts.hostname:
            raise ValueError("A URL da câmera precisa começar com rtsp:// ou rtsps://")
        if not self.username or parts.username:
            return raw_url

        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        credentials = quote(self.username, safe="")
        if self.password:
            credentials += f":{quote(self.password, safe='')}"
        netloc = f"{credentials}@{host}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


_PREFIX = "camera:"


def _read_parser(config_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuração da câmera não encontrada: {config_path}. "
            "Crie camera.ini a partir de camera.ini.example."
        )
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise ValueError(f"Arquivo de configuração inválido: {config_path}") from exc
    return parser


def _camera_from_section(name: str, camera) -> CameraConfig:
    kind = camera.get("kind", "rtsp").strip().lower()
    try:
        port = camera.getint("port", fallback=A9_DEFAULT_PORT)
        device = camera.getint("device", fallback=0)
        timeout = camera.getint("read_timeout_seconds", fallback=10)
    except ValueError as exc:
        raise ValueError(f"Número inválido na câmera '{name}'") from exc
    return CameraConfig(
        url=camera.get("url", "").strip(),
        username=camera.get("username", camera.get("usuario", "")).strip(),
        password=camera.get("password", camera.get("senha", "")),
        transport=camera.get("transport", camera.get("transporte", "tcp")).strip().lower(),
        read_timeout_seconds=timeout,
        name=name,
        kind=kind,
        model=camera.get("model", camera.get("modelo", "")).strip(),
        ip=camera.get("ip", "").strip(),
        port=port,
        did=camera.get("did", "").strip(),
        secret=camera.get("secret", camera.get("segredo", "")).strip(),
        device=device,
    )


def load_cameras(path: Path | None = None) -> tuple[list[CameraConfig], str]:
    """Every camera in camera.ini and the name of the one in use.

    A ``[camera:Name]`` section per camera; the older single ``[camera]`` section still reads
    (as "Câmera"). The active one is ``[cameras] active``, else the first.
    """
    config_path = path or Path.cwd() / "camera.ini"
    parser = _read_parser(config_path)
    cameras = []
    if parser.has_section("camera"):
        cameras.append(_camera_from_section(DEFAULT_CAMERA_NAME, parser["camera"]))
    for section in parser.sections():
        if section.lower().startswith(_PREFIX):
            name = section[len(_PREFIX):].strip()
            if name and all(name != c.name for c in cameras):
                cameras.append(_camera_from_section(name, parser[section]))
    if not cameras:
        raise ValueError(f"Nenhuma câmera em {config_path}")
    for camera in cameras:
        camera.validate()
    active = parser.get("cameras", "active", fallback="").strip()
    if all(active != c.name for c in cameras):
        active = cameras[0].name
    return cameras, active


def load_camera_config(path: Path | None = None) -> CameraConfig:
    """The camera in use."""
    cameras, active = load_cameras(path)
    return next(c for c in cameras if c.name == active)


def save_cameras(cameras: list[CameraConfig], active: str, path: Path | None = None) -> None:
    """Write every camera and the active one, keeping any other section (the older [camera] one is migrated)."""
    names = [c.name for c in cameras]
    if not cameras or len(set(names)) != len(names):
        raise ValueError("Os nomes das câmeras precisam ser únicos.")
    for camera in cameras:
        camera.validate()
    config_path = path or Path.cwd() / "camera.ini"
    parser = configparser.ConfigParser(interpolation=None)
    if config_path.is_file():
        parser.read(config_path, encoding="utf-8")
    for section in list(parser.sections()):
        if section == "camera" or section.lower().startswith(_PREFIX):
            parser.remove_section(section)
    parser["cameras"] = {"active": active if active in names else names[0]}
    for camera in cameras:
        parser[_PREFIX + camera.name] = {
            "kind": camera.kind,
            "model": camera.model,
            "url": camera.url.strip(),
            "username": camera.username.strip(),
            "password": camera.password,
            "transport": camera.transport,
            "read_timeout_seconds": str(camera.read_timeout_seconds),
            "ip": camera.ip.strip(),
            "port": str(camera.port),
            "did": camera.did.strip(),
            "secret": camera.secret,
            "device": str(camera.device),
        }
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    try:
        config_path.chmod(0o600)  # it may hold camera passwords and secrets
    except OSError:
        pass


def save_camera_config(config: CameraConfig, path: Path | None = None) -> None:
    """Save one camera (replacing the one with its name, or adding it) and make it the active one."""
    config_path = path or Path.cwd() / "camera.ini"
    try:
        cameras, _ = load_cameras(config_path)
    except (FileNotFoundError, ValueError):
        cameras = []
    cameras = [config if c.name == config.name else c for c in cameras]
    if all(c.name != config.name for c in cameras):
        cameras.append(config)
    save_cameras(cameras, config.name, config_path)


if __name__ == "__main__":  # python -m levainpeak.config: the stream URL from camera.ini, for ffplay/ffprobe
    print(load_camera_config().stream_url())
