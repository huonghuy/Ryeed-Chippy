# Host CHIPPY

Run one Python process behind an HTTPS reverse proxy, with a persistent data
directory. All browsers and the receiving computer use the same website. The USB
receiver stays connected to the receiving computer, which forwards its readings.

## Server startup

Copy the app, static/, and vendor/ to the server. Python 3.10+ is required. Keep
data outside ephemeral build directories. Example Linux command:

```sh
python3 /opt/chippy/app.py --host 127.0.0.1 --port 8765 --no-browser --data-dir /var/lib/chippy --public-url https://chippy.example.com
```

Replace paths and domain. The public URL must match the website origin exactly,
including nonstandard ports. Unknown Host headers and cross-origin browser writes
are rejected. Keep the backend private behind the proxy. Use a service manager
for startup and failure recovery; its account needs write access to data. Use
one process/instance without multiple workers or replicas against this database.

For LAN testing, --host 0.0.0.0 --public-url http://LAN-IP:8765 permits that address.
Browser USB on remote computers needs HTTPS; localhost also works. Configure the
website port as needed in the firewall, never the receiver's serial port.

## HTTPS reverse proxy

Example location in the Nginx HTTPS virtual host, with a valid certificate already
configured:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto https;
    client_max_body_size 12m;
    proxy_read_timeout 60s;
}
```

Preserve Host to match --public-url and redirect HTTP visitors to HTTPS. The app
sets Secure, HttpOnly, SameSite=Strict cookies for an HTTPS public URL and ignores
untrusted forwarded host headers. Serve at the domain root, not a subdirectory.
Allow browsers to load Cesium/workers from jsDelivr, Esri imagery, and Esri street-map
tiles. The standard Python HTTP server runs behind the reverse proxy; the app is
intended for a small flight team.

## First use

1. Start the server with persistent data storage.
2. Open the HTTPS website and log in with initial password **bpp**.
3. Use **Change password** before sharing the URL. The password hash survives
   restarts. Alternatively set CHIPPY_INITIAL_PASSWORD in the server environment
   before first startup; it does not override a saved password.
4. On the receiving computer, close Arduino Serial Monitor and run:

```powershell
py -3 receiver_uploader.py --server https://chippy.example.com --port COM4
```

5. Enter the current password and keep the computer awake. Other computers select
   **Follow live feed**, without admin login.

Alternatively, use **Admin Input → Connect USB & publish live** on the receiving
computer and keep that tab open. Browser capture queues retries in memory; Python
queues them on disk and runs without a browser. Both use 115200 baud. Only one
app can hold the USB port at a time.

## Operations

- Stop the server before backing up its data directory; SQLite uses WAL.
- Keep the uploader queue on the receiving computer. Restart after USB loss,
  password changes, login expiry (12 hours), or a replaced feed.
- Server restarts log out admins. Data, password hashes, browser ownership, and
  the shared-session selection persist.
- Publishing a CSV replaces the live feed with a recorded flight.
- Set storage/proxy request limits for your host, monitor disk use, and maintain
  backups. Only logged-in admins can add data to the server.
- There is no email recovery. Retain the password/database; recovery from a
  forgotten password requires server administration.

Your actual host, domain, and certificates are needed for deployment. This code
update does not create hosting.


Airspace update: include mission.py, static/mission.js, and static/geo.js.
Allow the Python server outbound HTTPS to services6.arcgis.com and tfr.faa.gov.
See MAP_FEATURES.md for reference-data freshness and overlay limitations.
