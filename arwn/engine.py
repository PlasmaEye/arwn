# Copyright 2016 Sean Dague
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import json
import logging
import time

import paho.mqtt.client as paho

from arwn import temperature
from arwn.vendor.RFXtrx import lowlevel as ll
from arwn.vendor.RFXtrx.pyserial import PySerialTransport

logger = logging.getLogger()

IS_NONE = 0
IS_TEMP = 1 << 0
IS_BARO = 1 << 1
IS_WIND = 1 << 2
IS_RAIN = 1 << 3


class SensorPacket(object):
    """Convert RFXtrx packet to native packet for ARWN"""

    def _set_type(self, packet):
        if self.stype != IS_NONE:
            return
        if isinstance(packet, ll.TempHumid):
            self.stype |= IS_TEMP
        if isinstance(packet, ll.TempHumidBaro):
            self.stype |= IS_TEMP
        if isinstance(packet, ll.RainGauge):
            self.stype |= IS_RAIN
        if isinstance(packet, ll.Wind):
            self.stype |= IS_WIND

    @property
    def is_temp(self):
        return self.stype & IS_TEMP

    @property
    def is_baro(self):
        return self.stype & IS_BARO

    @property
    def is_rain(self):
        return self.stype & IS_RAIN

    @property
    def is_wind(self):
        return self.stype & IS_WIND

    def __init__(self, stype=IS_NONE, bat=0, sig=0, sensor_id=0, **kwargs):
        self.stype = stype
        self.bat = bat,
        self.sig = sig
        self.sensor_id = sensor_id
        self.data = {}
        self.data.update(kwargs)

    def from_packet(self, packet):
        self._set_type(packet)
        self.bat = packet.battery
        self.sig = packet.rssi
        self.sensor_id = packet.id_string
        if self.stype & IS_TEMP:
            temp = temperature.Temperature("%sC" % packet.temp).as_F()
            self.data['temp'] = round(temp.to_F(), 1)
            self.data['dewpoint'] = round(temp.dewpoint(packet.humidity), 1)
            self.data['units'] = 'F'
        if self.stype & IS_BARO:
            self.data['pressure'] = packet.baro
            self.data['units'] = 'mbar'
        if self.stype & IS_RAIN:
            self.data['total'] = round(packet.raintotal / 25.4, 2)
            self.data['rate'] = round(packet.rainrate / 25.4, 2)
            self.data['units'] = 'in'
        if self.stype & IS_WIND:
            mps2mph = 2.23694
            speed = round(float(packet.average_speed) * mps2mph, 1)
            gust = round(float(packet.gust) * mps2mph, 1)
            self.data['direction'] = packet.direction
            self.data['speed'] = speed
            self.data['gust'] = gust
            self.data['units'] = 'mph'

    def as_json(self, **kwargs):
        data = dict(bat=self.bat, sig=self.sig, sensor_id=self.sensor_id)
        data.update(self.data)
        data.update(kwargs)
        return data


class MQTT(object):
    def __init__(self, server):
        client = paho.Client()
        # client.on_connect = on_connect
        # client.on_message = on_message
        client.connect(server, 1883)
        client.loop_start()
        self.client = client
        self.root = "arwn2"

    def send(self, topic, payload):
        topic = "%s/%s" % (self.root, topic)
        self.client.publish(topic, json.dumps(payload))


class Dispatcher(object):
    def __init__(self, device, names, server):
        self.transport = PySerialTransport(device, debug=True)
        self.transport.reset()
        self.names = names
        self.mqtt = MQTT(server)

    def loopforever(self):
        while True:
            event = self.transport.receive_blocking()
            logger.debug(event)

            if event is None:
                continue
            now = int(time.time())

            # special case. Temp / Humid / Barometer sensors are
            # turned into 2 sensors, barometer and a temp one.
            if isinstance(event.device.pkt, ll.TempHumidBaro):
                b_packet = SensorPacket(stype=IS_BARO)
                b_packet.from_packet(event.device.pkt)
                self.mqtt.send("barometer", b_packet.as_json(timestamp=now))

            # general case, temp, rain, wind
            packet = SensorPacket()
            packet.from_packet(event.device.pkt)

            if packet.is_temp:
                name = self.names.get(packet.sensor_id)
                if name:
                    topic = "temperature/%s" % name
                else:
                    topic = "unknown/%s" % packet.sensor_id
                self.mqtt.send(topic, packet.as_json(timestamp=now))

            if packet.is_wind:
                self.mqtt.send("wind", packet.as_json(timestamp=now))

            if packet.is_rain:
                self.mqtt.send("rain", packet.as_json(timestamp=now))
