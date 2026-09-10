# GUI Redesign — responsive operations console

This revision replaces the original `pyqtgraph.dockarea.DockArea` layout with a normal Qt splitter/tab layout designed for RHEL remote desktops and smaller browser-hosted VNC/RDP sessions.

## What changed

- Window size is calculated from Qt's actual `availableGeometry()` instead of forcing 1580x960.
- Fixed-width, scrollable chaos-control sidebar (300–390 px).
- Persistent top command bar with source state, ARM, RUN, and STOP.
- Seven compact telemetry cards stay visible while changing views.
- Workspace is split into `LIVE ANALYSIS`, `HEALTH + SCORECARD`, and `FAULT EVENTS` tabs.
- Live view gives the waterfall the most space, with spectrum/waveform below and a compact fault timeline.
- Removed the always-visible HistogramLUT from the waterfall because it consumed too much horizontal space.
- Waveform view is reduced from 20 ms to 4 ms and remotely-rendered samples are bounded to avoid the solid-block appearance.
- Fault controls are grouped into cards and placed inside scroll areas.
- Combo-box popups and item views receive explicit dark styling, preventing the white popup seen in the first build.
- Clean-vs-chaos comparison moved into a full-size health view instead of a squeezed right-side dock.
- Fault events now have a dedicated table with time, lane, fault, and detail.
- Added high-DPI Qt flags for RHEL remote sessions.

The chaos engine, scenarios, packet sources, safety boundary, and test-multicast behavior are unchanged.
