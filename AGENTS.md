# Repository Guidelines

## Project Purpose

LevainPeak uses Python, computer vision, and a simple Tk interface to monitor levain from an RTSP camera. The screen should show the stream, detected pot, estimated volume, and current phase. Record timestamped measurements in a session log. Detect phases from the trend: sustained growth, a probable peak after growth levels off, and degradation when the levain begins to fall. Do not assume every starter doubles; composition and conditions vary. Treat thermometer readings in frame as optional context, not a required signal.

## Project Structure

Keep RTSP capture, Tk presentation, pot/surface measurement, trend classification, and session logging in distinct modules. Put automated tests beside the relevant Python package or in a dedicated `tests/` directory. Keep sample images and video short, representative, and free of private camera footage unless specifically approved for the repository. Store local session logs and camera footage outside version control.

## Development and Testing

Use the Python environment and dependency instructions provided by the project as they are added. Keep Tk updates on its main thread and move stream reads and image analysis to a worker so the window stays responsive. Test image analysis with saved frames and phase detection with timestamped measurement sequences so results are repeatable without a live camera. Run the project’s configured test command before submitting changes; if it uses pytest, run `python -m pytest` from the repository root.

## Detection and Test Guidance

Detect a vessel's inner profile and the levain surface from the fixed camera view. Estimate relative volume by integrating the container's cross-sectional area up to the detected surface; for round pots, use the rotational-symmetry assumption, and account for tapered profiles instead of treating height as volume. A visible dark band may mark the initial level; also allow the user to activate a manual marker and place it with a mouse hover preview and click. A fixed camera and pot can support an approximate relative estimate without known focal length or physical dimensions, subject to perspective and shape assumptions; reporting milliliters requires a scale or known capacity calibration. Smooth noisy frame-to-frame measurements and distinguish a sustained rise from a short pause. Mark a peak only after growth has remained near-stationary for a configurable interval following a clear rising trend; mark degradation after a persistent decline. Keep thresholds and time windows configurable, and test pauses, irregular growth, lighting shifts, occlusion, and falling volume. Log session ID, timestamp, measurement, and phase. Validate thermometer reading separately and allow it to be unavailable.

## Style and Configuration

Follow existing Python conventions, use `snake_case` for modules and functions, and choose descriptive names. Store RTSP URLs and credentials in environment-based configuration; never commit secrets. Keep camera access and image processing errors visible in logs without exposing credentials.

## Commits and Pull Requests

Use concise, imperative commit subjects that describe the change. Pull requests should state the behavior being changed, explain how it was validated, link relevant project tasks, and include representative before/after frames or plots when they clarify detection changes.
