"""This computer's battery on Linux (engine/setup/hardware.py, linux_battery and /api/battery), read from sample power_supply
folders shaped like real laptops': energy or charge counters, one battery or two, a mouse's battery, a desktop with none, and
counters that say nothing useful. No real battery is needed, and nothing is written outside a temporary folder.

  python3 -B -m unittest app.tests.test_battery
"""
import io, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]
from engine.setup import hardware  # noqa: E402


class Supplies:
    """A /sys/class/power_supply of our own: supply(name, type=..., status=..., ...) writes one folder of one-line files."""
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def supply(self, name, **files):
        d = self.root / name
        d.mkdir()
        for k, v in files.items():
            (d / k).write_text(f'{v}\n', encoding='ascii')
        return self

    def read(self):
        return hardware.linux_battery(self.root)


class LinuxBattery(unittest.TestCase):
    def setUp(self):
        self.s = Supplies()

    def tearDown(self):
        self.s.dir.cleanup()

    def test_laptop_on_battery_energy_counters(self):
        self.s.supply('AC', type='Mains', online=0)
        self.s.supply('BAT0', type='Battery', present=1, status='Discharging', capacity=82,
                      energy_now=41230000, energy_full=50120000, power_now=9870000)
        b = self.s.read()
        self.assertEqual(b, {'on_battery': True, 'charging': False, 'percent': 82,
                             'seconds_left': round(41230000 / 9870000 * 3600), 'batteries': 1})   # 41.23 Wh at 9.87 W: about 4 h 11 min

    def test_charging_on_mains_charge_counters(self):
        self.s.supply('ADP1', type='Mains', online=1)
        self.s.supply('BAT1', type='Battery', status='Charging', charge_now=2200000, charge_full=4000000, current_now=1000000)
        self.assertEqual(self.s.read(), {'on_battery': False, 'charging': True, 'percent': 55, 'seconds_left': None, 'batteries': 1})

    def test_just_unplugged_before_the_status_catches_up(self):
        self.s.supply('AC', type='Mains', online=0)                          # some firmware says "Unknown" for a while after unplugging
        self.s.supply('BAT0', type='Battery', status='Unknown', energy_now=40000000, energy_full=50000000, power_now=8000000)
        b = self.s.read()
        self.assertEqual((b['on_battery'], b['charging'], b['seconds_left']), (True, False, 5 * 3600))

    def test_full_on_mains(self):
        self.s.supply('AC0', type='Mains', online=1)
        self.s.supply('BAT0', type='Battery', status='Full', capacity=100, energy_now=50000000, energy_full=50000000, power_now=0)
        self.assertEqual(self.s.read(), {'on_battery': False, 'charging': False, 'percent': 100, 'seconds_left': None, 'batteries': 1})

    def test_two_batteries_count_as_one(self):
        self.s.supply('BAT0', type='Battery', status='Discharging', energy_now=20000000, energy_full=40000000, power_now=5000000)
        self.s.supply('BAT1', type='Battery', status='Unknown', energy_now=10000000, energy_full=20000000, power_now=0)
        b = self.s.read()
        self.assertEqual((b['on_battery'], b['percent'], b['seconds_left'], b['batteries']), (True, 50, 6 * 3600, 2))

    def test_no_battery_of_its_own(self):
        self.assertIsNone(self.s.read())                                     # nothing there
        self.s.supply('AC', type='Mains', online=1)
        self.assertIsNone(self.s.read())                                     # a desktop
        self.s.supply('hid-00:11:22:33:44:55-battery', type='Battery', scope='Device', capacity=40, status='Discharging')
        self.assertIsNone(self.s.read())                                     # a wireless mouse's battery is not the computer's
        self.s.supply('BAT1', type='Battery', present=0)
        self.assertIsNone(self.s.read())                                     # an empty battery bay
        self.assertIsNone(hardware.linux_battery(self.s.root / 'missing'))

    def test_counters_that_say_little(self):
        self.s.supply('AC', type='Mains', online=0)
        self.s.supply('BAT0', type='Battery', status='Discharging', capacity=64, energy_now='garbage')
        self.assertEqual(self.s.read(), {'on_battery': True, 'charging': False, 'percent': 64, 'seconds_left': None, 'batteries': 1})

    def test_no_time_left_while_the_reading_settles(self):
        self.s.supply('BAT0', type='Battery', status='Discharging', energy_now=40000000, energy_full=50000000, power_now=0)
        self.assertIsNone(self.s.read()['seconds_left'])                     # just unplugged: no draw measured yet
        s2 = Supplies().supply('BAT0', type='Battery', status='Discharging', energy_now=40000000, energy_full=50000000, power_now=1)
        self.assertIsNone(s2.read()['seconds_left'])                         # 40 million hours: not a real estimate
        s2.dir.cleanup()

    def test_mixed_counters_fall_back_to_capacity(self):
        self.s.supply('BAT0', type='Battery', status='Discharging', capacity=70, energy_now=35000000, energy_full=50000000, power_now=7000000)
        self.s.supply('BAT1', type='Battery', status='Discharging', capacity=30, charge_now=1200000, charge_full=4000000, current_now=500000)
        b = self.s.read()
        self.assertEqual((b['percent'], b['seconds_left']), (50, None))     # µWh and µAh do not add up: the capacities do

    def test_percent_stays_in_range(self):
        self.s.supply('BAT0', type='Battery', status='Charging', energy_now=52000000, energy_full=50000000)
        self.assertEqual(self.s.read()['percent'], 100)


def mac(**over):
    """One internal battery as IOPSGetPowerSourceDescription describes it on a MacBook, with some values changed."""
    return {'Type': 'InternalBattery', 'Transport Type': 'Internal', 'Name': 'InternalBattery-0', 'Is Present': True,
            'Power Source State': 'Battery Power', 'Is Charging': False, 'Current Capacity': 80, 'Max Capacity': 100,
            'Time to Empty': 240, 'Time to Full Charge': 0, **over}


class MacBattery(unittest.TestCase):
    """mac_battery's reading of what macOS reports. The IOKit call itself (mac_power_sources) only runs on a Mac."""

    def test_laptop_on_battery(self):
        self.assertEqual(hardware.mac_battery([mac()]),
                         {'on_battery': True, 'charging': False, 'percent': 80, 'seconds_left': 240 * 60, 'batteries': 1})

    def test_charging_on_mains(self):
        got = hardware.mac_battery([mac(**{'Power Source State': 'AC Power', 'Is Charging': True, 'Current Capacity': 55,
                                           'Time to Empty': 0, 'Time to Full Charge': 70})])
        self.assertEqual(got, {'on_battery': False, 'charging': True, 'percent': 55, 'seconds_left': None, 'batteries': 1})

    def test_full_on_mains(self):
        got = hardware.mac_battery([mac(**{'Power Source State': 'AC Power', 'Current Capacity': 100, 'Time to Empty': 0, 'Is Charged': True})])
        self.assertEqual(got, {'on_battery': False, 'charging': False, 'percent': 100, 'seconds_left': None, 'batteries': 1})

    def test_no_time_left_while_macos_is_still_working_it_out(self):
        for minutes in (-1, 0, None, 8 * 24 * 60):         # -1 is macOS's "calculating"; more than a week is not a reading
            with self.subTest(minutes=minutes):
                self.assertIsNone(hardware.mac_battery([mac(**{'Time to Empty': minutes})])['seconds_left'])

    def test_no_battery_of_its_own(self):
        self.assertIsNone(hardware.mac_battery([]))                                          # a desktop Mac
        self.assertIsNone(hardware.mac_battery([mac(Type='UPS', **{'Transport Type': 'USB'})]))   # a UPS is not the computer's battery
        self.assertIsNone(hardware.mac_battery([mac(**{'Is Present': False})]))

    def test_capacity_not_in_percent_and_missing(self):
        self.assertEqual(hardware.mac_battery([mac(**{'Current Capacity': 2600, 'Max Capacity': 5200})])['percent'], 50)   # mAh on an older Mac
        self.assertIsNone(hardware.mac_battery([mac(**{'Current Capacity': None})])['percent'])
        self.assertEqual(hardware.mac_battery([mac(**{'Current Capacity': 140})])['percent'], 100)


class Route(unittest.TestCase):
    class H:
        def __init__(self, ip):
            self.client_address, self.command, self.wfile, self.status = (ip, 1), 'GET', io.BytesIO(), None
        def send_response(self, s): self.status = s
        def send_header(self, *a): pass
        def end_headers(self): pass

    def test_only_this_computer(self):
        h = self.H('192.0.2.20')
        hardware.handle_battery(h)
        self.assertEqual(h.status, 403)
        self.assertNotIn(b'percent', h.wfile.getvalue())

    def test_answer(self):
        fake = {'on_battery': True, 'charging': False, 'percent': 50, 'seconds_left': 3600, 'batteries': 1}
        with mock.patch.object(hardware, 'os_name', return_value='linux'), mock.patch.object(hardware, 'linux_battery', return_value=fake):
            h = self.H('127.0.0.1')
            hardware.handle_battery(h)
        self.assertEqual((h.status, json.loads(h.wfile.getvalue())), (200, {'supported': True, 'battery': fake}))
        with mock.patch.object(hardware, 'os_name', return_value='windows'):
            self.assertEqual(hardware.battery(), {'supported': False, 'battery': None})   # the Windows reader comes later
        with mock.patch.object(hardware, 'os_name', return_value='macos'), mock.patch.object(hardware, 'mac_battery', return_value=fake):
            self.assertEqual(hardware.battery(), {'supported': True, 'battery': fake})
        with mock.patch.object(hardware, 'os_name', return_value='macos'), mock.patch.object(hardware, 'mac_power_sources', side_effect=OSError('no IOKit')):
            self.assertEqual(hardware.battery(), {'supported': True, 'battery': None})
        with mock.patch.object(hardware, 'os_name', return_value='linux'), mock.patch.object(hardware, 'linux_battery', side_effect=OSError('odd driver')):
            self.assertEqual(hardware.battery(), {'supported': True, 'battery': None})     # a strange driver breaks nothing


if __name__ == '__main__':
    unittest.main()
