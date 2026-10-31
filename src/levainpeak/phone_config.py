"""Local settings for the phone view: where to send the data and the secret token."""

from __future__ import annotations

import configparser
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit


@dataclass(frozen=True)
class PhoneConfig:
    url: str
    token: str
    panel_url: str = ""  # where the panel is published (for example GitHub Pages); optional

    def __repr__(self) -> str:  # never let the token reach a log line or a traceback
        return f"PhoneConfig(url={self.url!r}, token=<{len(self.token)} caracteres>)"


def load_phone_config(path: Path | None = None) -> PhoneConfig:
    config_path = path or Path.cwd() / "phone.ini"
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuração do celular não encontrada: {config_path}. "
            "Crie phone.ini a partir de phone.ini.example."
        )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise ValueError(f"Arquivo de configuração inválido: {config_path}") from exc
    if not parser.has_section("phone"):
        raise ValueError(f"Seção [phone] ausente em {config_path}")
    section = parser["phone"]
    url = section.get("url", "").strip()
    token = section.get("token", "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("A URL do celular precisa começar com https://")
    if len(token) < 16:
        raise ValueError("O token do celular precisa ter pelo menos 16 caracteres")
    panel_url = section.get("panel_url", "").strip()
    if panel_url and urlsplit(panel_url).scheme != "https":
        raise ValueError("O panel_url do celular precisa começar com https://")
    return PhoneConfig(url=url, token=token, panel_url=panel_url)


def lan_address() -> str | None:
    """This computer's address on the local network, without sending anything."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def panel_links(config: PhoneConfig, port: int = 8000) -> list[tuple[str, str]]:
    """(where, link) pairs. The settings sit after the #, so they never reach the server."""
    fragment = f"#u={quote(config.url, safe='')}&t={quote(config.token, safe='')}"
    links = []
    if config.panel_url:
        base = config.panel_url if config.panel_url.endswith("/") else config.panel_url + "/"
        links.append(("Publicado", f"{base}{fragment}"))
    links.append(("No PC", f"http://localhost:{port}/{fragment}"))
    address = lan_address()
    if address:
        links.append(("No celular (mesma rede)", f"http://{address}:{port}/{fragment}"))
    return links


def main() -> None:
    config = load_phone_config()
    print("Para servir o painel localmente:  cd painel && python -m http.server 8000\n")
    for where, link in panel_links(config):
        print(f"{where}:\n  {link}\n")
    print("Estes links carregam o token: não os compartilhe.")


if __name__ == "__main__":
    main()
