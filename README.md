# CHIPPY shared ground station

New: FAA airspace layers, Choppies geofences, KML upload, and Dropdown viewer.
See **MAP_FEATURES.md** for controls, supported formats, data sources, and setup.

Python 3.10+ dashboard for the existing Arduino Uno CHIPPY_RECEIVER C1 output.
No firmware change is needed. The USB connection runs at **115200 baud**.

## Start locally

Double-click **START_DASHBOARD.cmd**, or run `py -3 app.py` in this folder. Open
**http://127.0.0.1:8765**. Restart Python after installing this update and refresh
the browser. Keep app.py, shared_feed.py, web_server.py, mission.py, static/, and vendor/
together. pySerial is bundled; requirements.txt can rebuild it if needed.

## Run with Docker

Before the first start, create a file named `.env` next to docker-compose.yml.
Git ignores it, so the password stays out of the repository.

```sh
CHIPPY_PUBLIC_URL=https://chippy.huyhuong.com
CHIPPY_INITIAL_PASSWORD=paste-a-long-random-password-here
```

- **CHIPPY_PUBLIC_URL** is the exact address people open: scheme, host, and any
  nonstandard port, with no path. Requests for any other address get a 403. Leave
  it out to use the site at http://localhost:8765 only.
- **CHIPPY_INITIAL_PASSWORD** is the admin password. `openssl rand -hex 24` makes a
  good one. It is read only when the database is first created, and leaving it out
  means the password is **bpp**. After that, use **Change password**; editing `.env`
  has no effect.

Then run `docker compose pull && docker compose up -d`, and run the same command
to update later. Data and the saved password live in the `chippy-data` volume and
survive updates. Each push to main publishes a new `linux/amd64` image through
GitHub Actions; to build from source instead, see the comment in docker-compose.yml.

The USB receiver stays on the receiving computer. Publish from the browser, or run
receiver_uploader.py with `--server` set to the same address as CHIPPY_PUBLIC_URL,
even on the host itself. LAN settings are explained in docker-compose.yml.

## Tabs and access

Anyone can watch the shared feed and use the private tools below without a
password. Only a logged-in admin can send data to the server.

- **User Input** (admin only): private USB receiver input or a labeled simulated
  demo, saved on the server and visible only to admins.
- **Upload CSV**: private playback of balloon SD logs or dashboard exports. Play,
  Pause, Restart, speed, seek, and Show full flight work in 2D and 3D. The file is
  read in the browser and never uploaded, so load it again after a page refresh.
- **Upload geofence** and **Upload KML**: private map overlays kept in this
  browser's storage and never uploaded. Admins can also publish them to all viewers.
- **Admin Login**: beside the tabs. Initial password: **bpp** unless
  CHIPPY_INITIAL_PASSWORD was set.
- **Admin Input**: after login, publish USB readings or a shared CSV. Also contains
  **Change password** and **Log out**. New publications replace the followed feed;
  previous shared flights remain available in the session selector.
- **Follow live feed**: default view. Everyone can see shared measurements, the
  path, packets, and recent serial output without login. New data and newly
  published sessions are checked once a second.

The serial monitor shows 300 recent raw lines, including setup messages and
malformed telemetry. Raw lines remain saved. Valid C1 packets populate the cards,
map, inspector, packet log, and export. Duplicate packets remain in the log but
are excluded from the path and derived speeds.

## Change password

Choose **Admin Login → Admin Input → Change password**. Enter the current password,
new password, and confirmation. Salted scrypt password hashes are saved in the
database and survive restarts. Initial bpp applies only to a fresh database.
Changing it logs out other admin browsers and receiver uploaders; the browser
making the change stays signed in. Logins otherwise last 12 hours and end when
the server restarts. Retain your password and back up data; there is no email
recovery flow.

## Receiving computer → website → viewers

The server runs app.py. The Uno stays connected to the receiving computer by USB;
the server does not need a serial device.

### Browser publishing

1. Open the website on the receiving computer. Close Arduino Serial Monitor,
   Serial Plotter, and other apps using the Uno port.
2. Log in and choose **Admin Input → Connect USB & publish live**.
3. Select the Uno in the browser's picker. Opening USB may reset it; allow setup
   to finish before packets arrive.
4. Keep this tab and computer awake. Other computers choose **Follow live feed**;
   they need no USB connection, Python, or admin login.

Browser USB needs Web Serial support, such as current desktop Chrome or Edge, and
HTTPS (localhost also works). It selects ports on the receiving computer, not the
hosted server. Outages are retried using an in-memory buffer of about 20,000 lines;
keep the tab open until buffered lines are sent. For a persistent retry queue,
use the Python uploader. Only one app can hold the USB port at a time.

### Python publishing (recommended for unattended capture)

On the receiving computer, open a terminal in this folder:

```powershell
py -3 receiver_uploader.py --server https://YOUR-WEBSITE --list-ports
py -3 receiver_uploader.py --server https://YOUR-WEBSITE --port COM4
```

Replace the address and COM4. Enter the current admin password at the prompt. It
is not placed in the command or saved in the queue. For a local test against a
server with no https public URL, use `--server http://127.0.0.1:8765`.

Unsent lines are saved in data/upload-queue.sqlite3 and network failures retry.
Repeated HTTP batches do not duplicate records within the same feed. After a
program stop or USB loss, unsent lines remain for the next run, which starts a
new shared session and sends the backlog first. Receipt times are server upload
times and may be delayed during outages. Speeds still use balloon sample uptime.
A new admin publication, changed password, or expired login stops the uploader;
rerun and log in again. Keep one uploader per queue file and receiver. Ctrl+C
stops capture. Reconnect explicitly after USB loss. The computer must stay awake.

## Ground speed

Ground speed in m/s is horizontal great-circle distance between valid GPS samples
divided by their difference in balloon sample uptime. Altitude is excluded. It is
an average between samples, not airspeed. Ascent is altitude change divided by
sample time; negative values mean descent. GPS jitter can affect both readings.

The first sample, duplicates, resets, and out-of-order samples show no derived
speed until a valid baseline exists. Senders and boots stay separate; missed
transmissions extend the interval. Ground speed appears in the card, inspector,
packet table, and exports. Old saved packets retain their stored fields; reimport
an old CSV to calculate speeds throughout its history.

## Interactive 3D flight path

Choose **3D** in Flight path. Drag to pan, wheel or right-drag to zoom, and
middle-drag or Ctrl + drag to tilt/rotate. Click points to inspect packets.
**Fit path** frames the track; **Follow latest** recenters new live positions.
Return to **2D** any time. The 2D map remains usable if 3D cannot load.

CesiumJS 1.121 loads on demand from jsDelivr and uses Esri World Imagery. Internet
and WebGL are needed, without a Cesium account or token. Heights are real-scale
meters with no 100,000-foot cap (**100,000 ft = 30,480 m**). The globe uses an
ellipsoid surface, not detailed terrain. Recorded GPS altitude is plotted directly
above that reference, without geoid correction or terrain-clearance calculation.
This is a flight visualization, not precise above-ground clearance. There is no
vertical exaggeration. Points lacking altitude appear only in 2D. Paths are
separated by sender and boot. A dashed line shows the latest point's globe height.

## CSV and storage

Supported headers: 13-column balloon CSV, older 10-column CSV without UTC,
21-column receiver CSV, and dashboard exports. UTF-8 files may be up to 8 MB /
50,000 rows. Invalid files are rejected before creating a session. Derived speeds
are recalculated. example_flight.csv is synthetic test data.

**Publish CSV** in Admin Input shares the full recorded flight immediately,
labeled as recorded data. **Upload CSV** stays in the browser, with replay controls. CSVs
lack raw serial setup messages, so their monitor is empty. SD logs lack reception
timestamps: replay times are estimated from sample uptime and labeled accordingly.
Original timezone-aware received_at fields are preserved. GPS time is UTC; receipt
times display in each browser's timezone. C1 altitude is in meters. A live feed is
stale after 15 seconds without an accepted packet; USB alone does not prove reception.

Data and password hashes reside in data/chippy.sqlite3. Keep this on persistent
storage and back it up with the app stopped. Existing databases are migrated
without deleting records; admins can access legacy unshared sessions. Existing
ignore rules exclude data/ and Python caches from Git.

## Hosting and validation

See **DEPLOYMENT.md** for Python hosting behind an HTTPS reverse proxy. GitHub
Pages alone cannot run the API, database, or live feed. This update does not
deploy hosting or push the GitHub repository.

```powershell
py -3 -m unittest -v
node --check static/app.js
node --check static/live.js
node --check static/globe.js
node --check static/local_files.js
```

Tests cover parsing, CSVs, fragmented serial data, ground speed, private access,
multiple viewers, password persistence, feed replacement, and retry deduplication.
test_local_files.py needs Node and checks that the browser's CSV, KML, and geofence
parsing agrees with the server's.
Check actual Uno USB permissions and radio reception with hardware before flight.
