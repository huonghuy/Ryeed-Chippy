import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

from app import Store, Receiver, make_handler
from mission import rectangle, parse_kml, MapConfig, Airspace, altitude
import test_shared_feed as support

KML = '''<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>&lt;img src=x onerror=alert(1)&gt;</name><Point><coordinates>-77,39,30000</coordinates><altitudeMode>absolute</altitudeMode></Point></Placemark>
<Placemark><name>Route</name><LineString><coordinates>-77,39,0 -76.9,39.1,30480</coordinates></LineString></Placemark>
<Placemark><name>Fence</name><Polygon><outerBoundaryIs><LinearRing><coordinates>-77,39 -76.9,39 -76.9,39.1 -77,39.1 -77,39</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>'''


class MissionTests(unittest.TestCase):
    def test_rectangle_any_order_and_threshold(self):
        fence = rectangle([[39,-77],[38,-76],[39,-76],[38,-77]],500)
        self.assertEqual((fence["south"],fence["north"],fence["west"],fence["east"]),(38,39,-77,-76))
        self.assertEqual(fence["warning_m"],500)

    def test_rectangle_rejects_degenerate_nonrectangular_and_dateline(self):
        for corners in ([], [[1,1]]*4, [[1,1],[2,2],[2,3],[1,2]], [[1,-179],[1,179],[2,-179],[2,179]], [[1,0],[1,1],[float('nan'),0],[2,1]]):
            with self.assertRaises(ValueError): rectangle(corners)
        with self.assertRaises(ValueError): rectangle([[1,1],[1,2],[2,1],[2,2]],-1)

    def test_kml_point_route_polygon_and_namespaces(self):
        data=parse_kml(KML,"../../flight.kml")
        self.assertEqual(data["name"],"flight.kml")
        self.assertEqual([f["geometry"]["type"] for f in data["features"]],["Point","LineString","Polygon"])
        self.assertEqual(data["features"][0]["geometry"]["coordinates"],[-77,39,30000])
        self.assertEqual(data["features"][0]["properties"]["altitude_mode"],"absolute")
        self.assertIn("<img",data["features"][0]["properties"]["name"])

    def test_kml_multigeometry_and_holes(self):
        text='<kml><Placemark><MultiGeometry><Point><coordinates>1,2</coordinates></Point><Point><coordinates>3,4</coordinates></Point></MultiGeometry></Placemark></kml>'
        self.assertEqual(len(parse_kml(text,"x.kml")["features"]),2)
        text=KML.replace('</outerBoundaryIs>','</outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>-76.99,39.01 -76.98,39.01 -76.98,39.02 -76.99,39.01</coordinates></LinearRing></innerBoundaryIs>')
        self.assertEqual(len(parse_kml(text,"x.kml")["features"][-1]["geometry"]["coordinates"]),2)

    def test_kml_rejects_external_entities_links_bad_coordinates_and_limits(self):
        bad=['<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///secret">]><kml/>','<kml><NetworkLink/></kml>','<kml><GroundOverlay/></kml>','<kml><Placemark><Placemark><Point><coordinates>1,2</coordinates></Point></Placemark></Placemark></kml>','<bad/>','<kml>',KML.replace('-77,39,30000','-777,39,30000'),KML.replace('-77,39,30000','NaN,39,30000'),'<kml/>','x'*(4*1024*1024+1)]
        for text in bad:
            with self.assertRaises(ValueError):parse_kml(text,"bad.kml")

    def test_config_is_persistent_and_failed_upload_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'db')
            maps=MapConfig(store)
            try:
                maps.update('shared',{'action':'geofence','corners':[[1,1],[1,2],[2,1],[2,2]]})
                maps.update('shared',{'action':'kml','kml':KML,'name':'x.kml'})
                before=maps.get('shared')
                with self.assertRaises(ValueError):maps.update('shared',{'action':'kml','kml':'bad'})
                self.assertEqual(maps.get('shared'),before)
                self.assertIsNone(maps.get('other')["geofence"])
                reopened=Store(Path(temp)/'db')
                try:self.assertEqual(MapConfig(reopened).get('shared'),before)
                finally:reopened.db.close()
            finally:store.db.close()

    def test_altitude_conversions_do_not_invent_msl_for_agl_or_unknown(self):
        self.assertEqual(altitude({'UPPER_VAL':10000,'UPPER_UOM':'FT','UPPER_CODE':'MSL'},'UPPER'),3048)
        self.assertEqual(altitude({'LOWER_CODE':'SFC'},'LOWER'),0)
        for code in ('AGL','FL','UNL',''):
            self.assertIsNone(altitude({'UPPER_VAL':100,'UPPER_UOM':'FT','UPPER_CODE':code},'UPPER'))

    def test_airspace_cache_stale_status_and_unknown_tfr_altitude(self):
        service=Airspace()
        data={'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Polygon','coordinates':[]},'properties':{'TITLE':'TFR test'}}]}
        with patch.object(service,'fetch',return_value=data) as fetch:
            result=service.query([-77,38,-76,39],['tfr'])
            self.assertIsNone(result['layers'][0]['features'][0]['properties']['ceiling_m'])
            service.query([-77,38,-76,39],['tfr']);self.assertEqual(fetch.call_count,1)
        for key,(stamp,cached) in list(service.cache.items()):service.cache[key]=(stamp-1000,cached)
        with patch.object(service,'fetch',side_effect=OSError('offline')):
            result=service.query([-77,38,-76,39],['tfr'])
            self.assertTrue(result['layers'][0]['stale'])
            self.assertEqual(result['layers'][0]['error'],'offline')
            self.assertEqual(len(result['layers'][0]['features']),1)

    def test_airspace_errors_and_query_limits_are_explicit(self):
        service=Airspace()
        with patch.object(service,'fetch',side_effect=OSError('offline')):
            result=service.query([-77,38,-76,39],['controlled','sua','tfr'])
            self.assertTrue(all(layer['stale'] and layer['error'] for layer in result['layers']))
        for bounds,kinds in (([-180,-90,180,90],['tfr']),([-77,38,-76,39],['bad']),([1,2],['tfr'])):
            with self.assertRaises(ValueError):service.query(bounds,kinds)


class MissionHTTPTests(unittest.TestCase):
    # Reuse helpers without inheriting/repeating the original test cases.
    setUp=support.SharedFeedTests.setUp
    tearDown=support.SharedFeedTests.tearDown
    client=staticmethod(support.SharedFeedTests.client)
    request=support.SharedFeedTests.request
    assert_status=support.SharedFeedTests.assert_status
    login=support.SharedFeedTests.login

    def test_server_overlays_are_admin_only_and_shared(self):
        body={'action':'geofence','corners':[[38,-77],[38,-76],[39,-77],[39,-76]],'shared':False}
        self.assert_status(401,self.viewer,'/api/map-config',body)
        self.assert_status(401,self.viewer,'/api/map-config',{**body,'shared':True})
        self.assertIsNone(self.request(self.viewer,'/api/map-config')['geofence'])
        self.login()
        self.request(self.publisher,'/api/map-config',{**body,'shared':True})
        self.assertIsNotNone(self.request(self.viewer,'/api/map-config')['geofence'])
        self.assert_status(401,self.viewer,'/api/map-config',{'action':'clear_geofence','shared':True})

    def test_kml_publish_preserves_geofence_and_clear_only_selected_overlay(self):
        self.login()
        self.request(self.publisher,'/api/map-config',{'action':'geofence','corners':[[38,-77],[38,-76],[39,-77],[39,-76]],'shared':True})
        self.request(self.publisher,'/api/map-config',{'action':'kml','kml':KML,'name':'flight.kml','shared':True})
        config=self.request(self.viewer,'/api/map-config')
        self.assertIsNotNone(config['geofence']);self.assertEqual(len(config['kml']['features']),3)
        self.request(self.publisher,'/api/map-config',{'action':'clear_kml','shared':True})
        config=self.request(self.viewer,'/api/map-config')
        self.assertIsNotNone(config['geofence']);self.assertIsNone(config['kml'])

if __name__=='__main__':unittest.main()
