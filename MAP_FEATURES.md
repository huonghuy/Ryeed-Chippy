# Airspace, Choppies geofences, KML, and Dropdown viewer

Restart START_DASHBOARD.cmd and refresh the browser after installing this update.
Keep the new mission.py module and static/mission.js / static/geo.js with the app.

## Layer switches

The controls above Flight path work independently:

- **Airspace Toggle**: show/hide the FAA reference overlays. Initially off.
- **Choppies Toggle**: show/hide the geofence, payload ground marker, nearest-edge
  line, and boundary status. This does not change anything onboard.
- **KML overlays**: show/hide the uploaded KML independently of the other layers.
- **Dropdown viewer**: switch on the 3D altitude curtains and ground trace. Switch
  it off to return to the ordinary 3D position/path display. 2D remains available.
- **Shared flight overlays / My overlays**: choose the overlays published by the
  admin or your own private geofence/KML. This does not change the flight data.

## Upload geofence

1. Open **Upload geofence**.
2. Enter four corner pairs in **latitude, longitude** decimal degrees. The corner
   order does not matter. Use negative longitude for locations west of Greenwich.
3. The corners must describe a latitude/longitude-aligned rectangle: two distinct
   latitudes and two distinct longitudes, with all four combinations. Rotated,
   zero-area, polar, and dateline-spanning rectangles are rejected.
4. Set **Warn within (m)**, then choose **Save to my view**.
5. If signed in as admin, **Publish geofence to viewers** saves it to the shared
   flight overlays. Other viewers receive the update within about five seconds.

An orange ground overlay shows the rectangle in 2D and 3D. A colored marker shows
the payload's ground position, with a line to the nearest edge. The banner shows
**inside**, **near edge**, **outside**, or **unknown**, including distance in meters.
The warning threshold only changes the dashboard's display; it is not an onboard
cutdown configuration. Geofence calculations are horizontal and have no altitude
ceiling. They use a spherical Earth approximation.

Live boundary status requires the newest packet to have a valid GPS position and
a server receipt timestamp within 15 seconds. Missing fix, duplicate latest data,
or stale telemetry yields **unknown**, rather than retaining a green inside status.
The last ground marker may remain visible in gray. Recorded CSV/replay status is
explicitly labeled **RECORDED POSITION**, using the displayed replay position.

**Choppies handles automatic cutdown onboard.** This dashboard sends no cutdown
commands and does not upload its rectangle into the payload's firmware. Configure
and verify the onboard geofence separately using the onboard system's procedures.
Receipt times can be delayed by uploader queues; recently uploaded backlog is not
proof of the payload's current location.

## Upload KML

1. Open **Upload KML** and choose a self-contained **.kml** file up to 4 MB.
2. Choose **Load to my view**, or **Publish KML to viewers** when signed in as admin.
3. Points, LineStrings, Polygons with holes, and MultiGeometry are supported in
   both maps. The file can contain up to 1,000 placemarks/features and 20,000 vertices.
4. To use a KML rectangle as the Choppies geofence, select its polygon in the list
   and choose **Use selected rectangle as geofence**. It must have exactly four
   corners, no holes, and pass the same rectangular-boundary checks.

KML stores coordinates as **longitude, latitude, altitude**, unlike the manual
geofence form. Network links, external image overlays, models, tours, and tracks
are rejected; KMZ is not supported in this release. Names are displayed as text,
and remote files referenced by a KML are not fetched. Styles/descriptions are not
imported; overlays use a consistent teal map style. Use a plain .kml export from
your mapping application, rather than a KMZ archive.

Absolute altitude is plotted in meters. clampToGround uses the globe/reference
surface; relativeToGround is relative to that same reference surface, without
detailed terrain or geoid correction. The 2D map shows the horizontal footprint.

Shared overlays are saved in the server database and survive restarts; only admins
can change them. Private overlays are parsed and stored in the browser and never
reach the server, so clearing browser data removes them and they do not follow you
to another computer. Loading a KML does not replace the existing geofence. Clearing
a KML does not clear the geofence, and vice versa.

## Airspace modes

Switch on **Airspace Toggle**, then choose:

- **Controlled B / C / D**: FAA Class B, C, and D boundaries. Blue/purple tones.
- **Special Use Airspaces**: FAA special-use boundaries. Amber.
- **TFR Airspace**: FAA TFR reference polygons. Red.

In 2D, click an airspace polygon to see its name and available vertical limits.
In 3D, controlled/SUA boundaries with convertible MSL/SFC limits become translucent
volumes. AGL, flight-level, unlimited, or missing limits are not invented as MSL
heights; those features remain footprints. The FAA TFR geometry feed does not
provide vertical limits, so TFRs are ground footprints. SUA boundary presence does
not indicate current activation. TFR polygons can include scheduled/extended
restrictions; consult the official details for effective times and vertical limits.

Airspace loads for the current visible region. Zoom in to a view spanning at most
8 degrees latitude/longitude. Dateline-spanning queries are not supported. Panning
the 2D map reloads the region; in 3D choose **Refresh airspace here** after moving
the camera. While visible, enabled airspace also refreshes once per minute.
Class/SUA results may be cached for five minutes and TFR geometry for one minute.
The banner shows fetch time and reports unavailable, stale, or partial results.
Missing data must not be interpreted as clear airspace. These map overlays are
reference displays, not a flight authorization or an onboard cutdown trigger.

Sources:

- FAA AIS Class Airspace: https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Class_Airspace/FeatureServer/0
- FAA AIS Special Use Airspace: https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Special_Use_Airspace/FeatureServer/0
- FAA TFR viewer: https://tfr.faa.gov/tfr3/
- FAA TFR geometry: https://tfr.faa.gov/geoserver/TFR/ows

The server needs outbound HTTPS access to services6.arcgis.com and tfr.faa.gov.
Viewer browsers need jsDelivr/Cesium and Esri imagery/street-map access. The 2D
basemap now uses Esri World Street Map; the previous provider was returning blocked
tiles during testing. Both maps preserve source attribution.

## Dropdown viewer

The reference-style display adds a translucent curtain down to the ellipsoid
surface, with **magenta ascent**, **orange descent**, a ground trace, and start /
latest markers. The top line remains the actual recorded 3D track. It works with
CSV replay and live input, with no vertical exaggeration or 100,000-ft display cap.
Separate sender/boot paths stay separate. It shows where telemetry has been;
it does not predict a future flight or landing location.

## Checks

```powershell
py -3 -m unittest -v
node test_geo.cjs
node --check static/mission.js
node --check static/globe.js
```

Automated checks cover rectangle validation, KML parsing and rejected external
content, shared/private access, persistence, FAA source failure/cache behavior,
altitude conversion, and boundary/stale-GPS calculations. Browser checks used
synthetic flight data and a synthetic KML. Actual receiver/onboard integration
and the eventual hosted server still need their own checks.
