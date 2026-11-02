# CHIPPY local ground station

Python reads the Uno's USB serial output and saves every accepted packet. A local
browser dashboard plots GPS positions and shows each packet's measurements.
Use this with the supplied **CHIPPY_RECEIVER** firmware on an **Arduino Uno R3**.
No changes to the balloon or receiver firmware are required.

## Start on Windows

1. Extract the complete ZIP to a normal folder. Keep `app.py`, `static`, and `vendor` together.
2. Double-click **START_DASHBOARD.cmd**. Python 3.10 or newer is required; Python 3.11
   is already installed on the computer used to prepare this package.
3. The browser opens **http://127.0.0.1:8765**. Keep the Python/terminal window open.
4. Select **Try demo** to explore with clearly labeled simulated data. Demo data is
   saved in its own session, separate from receiver data.
5. For live reception, connect the Uno by USB and **close Arduino Serial Monitor**,
   Serial Plotter, and any other app holding its COM port.
6. Refresh the USB port list, select the Uno's COM port, and choose **Connect receiver**.
   The app reads **115200 baud**; this is the Uno-to-computer rate, not the radio's 9600 rate.
7. Opening the USB connection normally resets the Uno. Allow its radio setup to
   finish; the balloon sends a packet approximately every five seconds.

The port selector never automatically opens a device. Choose the Uno deliberately.
No Uno was connected during development; real hardware reception remains to be tested.

## Upload a flight CSV and replay it

1. Open the **Upload CSV** tab.
2. Choose your `fulltest.csv` from the balloon SD card, or a CSV exported by this dashboard.
3. Choose **Load flight**. The file is saved as its own CSV session; the original
   file is not changed. No Arduino connection is needed.
4. Choose **Play** to animate the flight. **Pause**, **Restart**, the packet slider,
   and **1× / 10× / 50× / 100×** speed settings control playback. Timing follows
   differences in balloon sample uptime. **Show full flight** displays every position.
5. Click any map point or row to inspect the measurements. Export CSV downloads the
   entire imported flight, even while the replay has only shown its first few points.

The included **example_flight.csv** contains 30 synthetic records near College Park
for trying this feature. It is demonstration data, not a real flight.

Supported headers: the current 13-column balloon CSV, the older 10-column balloon
CSV without UTC fields, the 21-column Uno CSV with headers, and dashboard CSV exports.
Extra export columns are accepted. Ascent is recalculated from the flight data.
Files must be UTF-8 CSV, up to 8 MB and 50,000 data rows. Invalid rows report an
error and the entire upload is rejected, avoiding partly imported flights.

SD logs do not record ground reception time. The app labels their generated
timestamps **Estimated replay time**, anchored to import time and spaced using
sample uptime. GPS hour/minute/second are displayed separately as UTC. Original
timezone-aware `received_at` timestamps in receiver exports are preserved.
SD logs also lack RSSI/SNR, so those fields remain blank; BME/SD flags for imported
SD rows are inferred from the logged data rather than new live hardware checks.
Imported sessions stay in the session selector after restarting the app. Selecting
one restores its replay controls. Importing does not stop an existing serial capture;
live packets continue saving to their separate session in the background.

## Use VS Code (recommended for editing)

Choose **File → Open Folder**, select this extracted folder, then open
**Terminal → New Terminal** and run:

```powershell
py -3 app.py
```

VS Code is an editor, not a requirement for running the station. The program runs
in Python and the display runs in your browser. It does not run on the Arduino.
The serial library is included in `vendor`; no pip install is needed for this ZIP.
If you intentionally rebuild without `vendor`, use `py -3 -m pip install -r requirements.txt`.

If port 8765 is already in use, close the other dashboard instance or run:

```powershell
py -3 app.py --port 8766
```

Use **Disconnect** before reopening Arduino Serial Monitor. Close the dashboard
terminal or press **Ctrl+C** to stop the server. Closing only the browser does not
stop reception; this allows packets to keep saving in the background.

## Inspect a flight

- Valid GPS packets add map points and a connecting path. Click a point or a packet
  row to see temperature, GPS altitude, ascent rate, reception time, GPS UTC,
  coordinates, pressure, humidity, satellites, RSSI/SNR, and BME/SD flags.
- The four top cards always describe the latest packet. The inspector can stay
  on an older packet while new data arrives.
- **Fit path** frames all positions. **Follow latest** follows new positions;
  selecting an older packet pauses automatic following until re-enabled.
- Packets without a GPS fix are still logged and listed, but do not create positions.
- The list shows the latest 500 rows; map/history and CSV export retain all accepted
  packets. Older sessions can be reopened with the session selector.
- A connected serial port does not prove radio reception. After 15 seconds without
  a new packet, the live connection badge shows that packets are stale.
- **Export CSV** downloads all received records in the selected session, including
  local receipt timestamps and calculated ascent rate.

## Times and ascent

Reception time is recorded as UTC on this computer when a complete serial line is
read, then displayed in the browser's local timezone. It is not an exact radio
arrival timestamp; operating-system/USB buffering can add delay. Keep the computer's
clock accurate. Exported `received_at` is an ISO 8601 UTC timestamp with timezone.
The balloon's hour/minute/second columns remain GPS UTC, without a date column.

Ascent in m/s is `(current GPS altitude - previous valid GPS altitude) / elapsed
balloon sample time`. Positive is ascent; negative is descent. It is an average
between samples and may be noisy because GPS altitude is noisy. It stays blank
until two valid samples are available. Missed transmissions extend the interval,
instead of assuming a constant five seconds. Duplicates are saved but excluded from
rate/path calculations. A backwards sample uptime (restart, timer wrap, or an
out-of-order record) begins a new track segment and resets the rate calculation.
Reconnecting the receiver starts a new session and resets the rate baseline.

## Saved data and maps

Real and demo sessions are saved to **data/chippy.sqlite3** beside the app.
They remain after closing and restarting the program. Back up the `data` folder
with the app stopped. CSV export is the convenient human-readable flight log.
The program never deletes sessions automatically. Demonstration/test data used
to verify the app is not included in this package.

The web server listens only on this computer at **127.0.0.1**. It is not deployed
publicly. The HTML, scripts, map library and serial library are all included locally.
The map background uses online OpenStreetMap tiles, so it needs an internet
connection. Without internet, packet reception, logging, inspection and CSV export
still work; map points can appear over a blank background. Tile requests reveal the
viewed map area to the tile provider. No telemetry is uploaded to a hosting service.

## Troubleshooting

- **No ports:** reconnect USB, refresh the list, and verify Windows recognizes the Uno.
- **Port cannot open:** close Arduino Serial Monitor/Plotter and other dashboard instances.
- **Connected but no packets:** verify the Uno has CHIPPY_RECEIVER, ground radio is at
  9600 baud, both radio settings match, and the balloon reports transmissions.
- **Malformed lines:** this app expects the 21-column C1 CSV emitted by CHIPPY_RECEIVER,
  not raw `+RCV` radio lines or the balloon's debug output. Check you selected the Uno.
- **Lost USB connection:** reconnect, refresh ports, and choose Connect. Saved data remains.
- **No position:** a GPS fix is required for map points. Other measurements can still arrive.

## Validation and dependencies

Run checks from this folder with `py -3 -m unittest -v`.
Packet validation, missing fixes/time, sample-time ascent, duplicate/restart handling,
persistent sessions, HTTP/CSV export and local-access checks have automated coverage.
The dashboard was also checked in a browser with synthetic packets, map tiles,
packet selection and disconnect/history behavior. JavaScript syntax checked with Node.
Serial-library loopback works; no physical Uno/radio link was tested here.

Included dependencies retain their licenses:
- pySerial **3.5**, Python serial interface: https://pyserial.readthedocs.io/en/latest/
- Leaflet **1.9.4**, map display: https://leafletjs.com/
- Online background: https://www.openstreetmap.org/copyright

Python: https://www.python.org/downloads/ · VS Code: https://code.visualstudio.com/
#   R y e e d - C h i p p y  
 