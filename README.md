# LevainPeak

LevainPeak is a Python computer-vision project with a simple Tk screen for monitoring levain from an RTSP camera stream. The screen should show the live stream, detected pot, estimated volume, and current phase. It aims to identify a likely peak after sustained growth levels off and detect a persistent fall that signals degradation. A doubling threshold is not assumed because starters and conditions differ.

## Intended flow

1. Read frames from the RTSP camera stream without blocking the Tk interface.
2. Detect the pot and measure the levain consistently in a stable camera view.
3. Estimate the pot's inner profile and integrate its cross-sectional area up to the levain surface, accounting for tapered pots.
4. Smooth measurements and report growth, a probable peak, or persistent decline.
5. Log each session's timestamp, measurement, and phase. If visible and readable, thermometer data may be recorded as optional context.

Keep stream acquisition, image measurement, trend analysis, session logging, and presentation separate. The first vision pass searches for a prominent fully visible vessel using paired side edges and horizontal bands, detects the levain surface from the visible color transition, and estimates filled volume by integrating the squared width profile. It refreshes the estimate twice per second while the UI keeps displaying the live stream. A dark initial-level band is used as a growth baseline when visible; the user can activate “Marcar início”, hover to preview a horizontal line, then click to set or adjust that baseline manually.

Under “Marcar Regiões”, use “Pote” to draw a search region around the vessel and “Termômetro” to select the display. The selected areas remain clear while the rest of the preview is dimmed; processing uses the selected regions. “Automático” clears the pot region. The mouse wheel zooms the preview. Set the initial levain level with “Marcar início”. Before starting a session, “Hora da mistura” accepts a local date and time (`DD/MM/AAAA HH:MM`). Starting asks for a session name. The CSV records timestamped measurements, optional temperature, phase, and session name; a companion JSON file stores the pot and thermometer regions for later resumption.

Under “Arquivos”, “CSV” opens the current session log (or lets you choose one), and “Retomar” continues appending to a previous CSV (or start the app with `--resume`). Resumption restores its saved regions and replays its measurements to reconstruct the growth phase. This CSV format keeps the measurements easy to plot later, for example to compare levain performance across sessions. Separate app instances can monitor different pots in one camera frame by selecting different regions; whether the camera accepts simultaneous RTSP clients depends on its configuration.

This is an early vision prototype. Its displayed percentage is an approximate fraction of the visible container capacity, not milliliters. Backlighting, busy backgrounds, and labels over the levain surface can still make surface detection unstable. Prefer diffuse, steady lighting, a plain background, and a visible pot wall; validate the detected outline and surface on different pot shapes and frames before relying on session history.

With a fixed camera and pot, the image can support an approximate relative volume estimate without knowing the camera's focal length or the pot's physical dimensions, subject to perspective and shape assumptions. Reporting an absolute volume such as milliliters requires a known scale or capacity calibration. The common round-pot case assumes a rotationally symmetric profile; the algorithm should still support tapered walls.

## Local environment

The project uses a local Python virtual environment at `.venv/`. Activate it with:

```sh
source .venv/bin/activate
pip install -e .
cp camera.ini.example camera.ini
python -m levainpeak.app
python -m levainpeak.app --resume   # retoma a sessão gravada mais recentemente
```

The local `camera.ini` contains the RTSP URL and credentials and is ignored by Git.

Do not commit camera credentials, private footage, virtual environments, or generated model artifacts. See [AGENTS.md](AGENTS.md) for contribution guidance.

Licensed under the [MIT License](LICENSE).

## Acompanhar pelo celular

O app pode enviar, a cada 2 ou 4 minutos, o estado atual, os pontos novos, o gráfico e uma imagem reduzida do pote para
uma planilha do Google (por um Apps Script), e um painel só de leitura mostra isso no celular. Veja
`docs/apps-script/Code.gs`, `painel/LEIA-ME.md` e `docs/estado-do-projeto.md`. A URL e o token ficam em `phone.ini`,
fora do git. O painel é publicado de graça em https://lexrupy.github.io/levainpeak/ (GitHub Pages, a partir da pasta `painel/`); ele não contém segredo, e o endereço do Apps Script e o token ficam só no navegador de quem o abre.
