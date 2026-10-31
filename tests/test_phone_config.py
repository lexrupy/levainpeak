import pytest

from levainpeak.phone_config import PhoneConfig, load_phone_config, panel_links

URL = "https://script.google.com/macros/s/AKfycbTESTE/exec"
TOKEN = "t" * 24


def write(tmp_path, text):
    path = tmp_path / "phone.ini"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_the_url_and_the_token(tmp_path):
    config = load_phone_config(write(tmp_path, f"[phone]\nurl = {URL}\ntoken = {TOKEN}\n"))
    assert (config.url, config.token) == (URL, TOKEN)


@pytest.mark.parametrize("text, message", [
    ("", "Seção"),
    (f"[phone]\nurl = http://exemplo.com/exec\ntoken = {'t' * 24}\n", "https"),
    (f"[phone]\nurl = {URL}\ntoken = curto\n", "16 caracteres"),
    (f"[phone]\nurl = {URL}\n", "16 caracteres"),
])
def test_rejects_an_incomplete_or_unsafe_config(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_phone_config(write(tmp_path, text))


def test_a_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match="phone.ini.example"):
        load_phone_config(tmp_path / "nao-existe.ini")


def test_the_token_never_shows_in_repr_or_str():
    config = PhoneConfig(URL, TOKEN)
    assert TOKEN not in repr(config) and TOKEN not in str(config)


def test_links_carry_the_settings_after_the_hash_so_they_stay_off_the_server():
    for _where, link in panel_links(PhoneConfig(URL, TOKEN), port=8123):
        path, _, fragment = link.partition("#")
        assert path.endswith(":8123/") and TOKEN not in path and URL not in path
        assert fragment.startswith("u=https%3A%2F%2Fscript.google.com") and f"t={TOKEN}" in fragment


def test_the_published_panel_address_is_optional_and_comes_first_in_the_links(tmp_path):
    plain = load_phone_config(write(tmp_path, f"[phone]\nurl = {URL}\ntoken = {TOKEN}\n"))
    assert plain.panel_url == "" and all(where != "Publicado" for where, _ in panel_links(plain))
    published = load_phone_config(write(
        tmp_path, f"[phone]\nurl = {URL}\ntoken = {TOKEN}\npanel_url = https://lexrupy.github.io/levainpeak\n"))
    where, link = panel_links(published)[0]
    assert where == "Publicado" and link.startswith("https://lexrupy.github.io/levainpeak/#u=")


def test_an_insecure_panel_address_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="panel_url"):
        load_phone_config(write(tmp_path, f"[phone]\nurl = {URL}\ntoken = {TOKEN}\npanel_url = http://exemplo.com/\n"))
