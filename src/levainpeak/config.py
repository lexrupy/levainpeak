"""Local camera configuration."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass(frozen=True)
class CameraConfig:
    url: str
    username: str = ""
    password: str = ""
    transport: str = "tcp"
    read_timeout_seconds: int = 10

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


def load_camera_config(path: Path | None = None) -> CameraConfig:
    config_path = path or Path.cwd() / "camera.ini"
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
    if not parser.has_section("camera"):
        raise ValueError(f"Seção [camera] ausente em {config_path}")

    camera = parser["camera"]
    config = CameraConfig(
        url=camera.get("url", "").strip(),
        username=camera.get("username", camera.get("usuario", "")).strip(),
        password=camera.get("password", camera.get("senha", "")),
        transport=camera.get("transport", camera.get("transporte", "tcp")).strip().lower(),
        read_timeout_seconds=camera.getint("read_timeout_seconds", fallback=10),
    )
    config.stream_url()
    return config
