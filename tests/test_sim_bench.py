"""The simulated bench over real sockets: stimulus, response, noise and faults."""

import random
import socket
import time
import unittest

from benchrun.errors import PlanError
from benchrun.sim.bench import PORT_ORDER, SimBench, parse_fault
from benchrun.sim.instruments import Fault
from benchrun.transport import TcpTransport, parse_address


class Client:
    def __init__(self, address, timeout_s=2.0):
        host, port = parse_address(address)
        self.transport = TcpTransport(host, port, timeout_s)
        self.transport.open()

    def send(self, text):
        """Write, then wait until the instrument has acted on it.

        Each instrument has its own socket, so without this a reading on another
        connection could overtake the setting it depends on.
        """
        self.transport.write_line(text)
        self.ask("*OPC?")

    def ask(self, text):
        self.transport.write_line(text)
        return self.transport.read_line()

    def number(self, text):
        return float(self.ask(text))

    def close(self):
        self.transport.close()


class BenchCase(unittest.TestCase):
    bench_args = {"noise": 0.0}

    def setUp(self):
        self.bench = SimBench(**self.bench_args).start()
        self.addCleanup(self.bench.stop)
        self.clients = {}
        for kind, address in self.bench.addresses.items():
            client = Client(address)
            self.clients[kind] = client
            self.addCleanup(client.close)

    def __getattr__(self, name):
        clients = self.__dict__.get("clients", {})
        if name in clients:
            return clients[name]
        raise AttributeError(name)


class Stimulus(BenchCase):
    def power(self, volts=12.0, amps=0.0):
        self.psu.send(f"VOLT {volts};CURR 3;OUTP ON")
        self.eload.send(f"CURR {amps};INP ON")

    def test_each_instrument_identifies_itself(self):
        models = {"dmm": "SIM-DMM", "psu": "SIM-PSU", "eload": "SIM-ELOAD", "scope": "SIM-SCOPE", "siggen": "SIM-SIGGEN"}
        for kind, model in models.items():
            self.assertIn(model, getattr(self, kind).ask("*IDN?"))

    def test_output_is_zero_until_the_supply_is_on(self):
        self.assertEqual(self.dmm.number("MEAS:VOLT:DC?"), 0.0)
        self.power()
        self.assertEqual(self.dmm.number("MEAS:VOLT:DC?"), 5.0)

    def test_output_follows_the_line(self):
        self.power(volts=7.0)
        low = self.dmm.number("MEAS:VOLT:DC?")
        self.psu.send("VOLT 20")
        high = self.dmm.number("MEAS:VOLT:DC?")
        self.assertAlmostEqual(low, 4.995, places=7)
        self.assertAlmostEqual(high, 5.008, places=7)

    def test_output_follows_the_load(self):
        self.power(amps=1.0)
        self.assertAlmostEqual(self.dmm.number("READ?"), 4.985, places=7)
        self.assertAlmostEqual(self.eload.number("MEAS:CURR?"), 1.0, places=7)
        self.assertAlmostEqual(self.psu.number("MEAS:CURR?"), 1.003, places=7)

    def test_dropout_follows_the_input_down(self):
        self.power(volts=4.0, amps=1.0)
        self.assertAlmostEqual(self.dmm.number("MEAS:VOLT:DC?"), 4.0 - 0.35 - 0.25, places=7)

    def test_below_undervoltage_the_regulator_is_off(self):
        self.power(volts=2.0, amps=0.0)
        self.assertEqual(self.dmm.number("MEAS:VOLT:DC?"), 0.0)
        self.assertEqual(self.psu.number("MEAS:CURR?"), 0.0)

    def test_regulator_current_limit_collapses_the_output(self):
        self.power(amps=2.0)
        self.assertEqual(self.dmm.number("MEAS:VOLT:DC?"), 0.0)

    def test_supply_current_limit_is_reported(self):
        self.power(amps=1.0)
        self.assertEqual(int(self.psu.number("STAT:OPER:COND?")) & 0x400, 0)
        self.psu.send("CURR 0.5")
        self.assertEqual(int(self.psu.number("STAT:OPER:COND?")) & 0x400, 0x400)
        self.assertEqual(self.psu.number("MEAS:CURR?"), 0.5)
        self.assertEqual(self.dmm.number("MEAS:VOLT:DC?"), 0.0)

    def test_load_off_draws_nothing(self):
        self.power(amps=1.0)
        self.eload.send("INP OFF")
        self.assertEqual(self.eload.number("MEAS:CURR?"), 0.0)
        self.assertEqual(self.dmm.number("MEAS:VOLT:DC?"), 5.0)

    def test_multimeter_current_function_reads_the_load(self):
        self.power(amps=0.25)
        self.dmm.send("CONF:CURR:DC 1")
        self.assertAlmostEqual(self.dmm.number("READ?"), 0.25, places=7)
        self.assertEqual(self.dmm.ask("CONF?"), '"CURR"')

    def test_multimeter_overload_below_the_signal(self):
        self.power()
        self.dmm.send("CONF:VOLT:DC 1")
        self.assertGreaterEqual(self.dmm.number("READ?"), 9.9e37)

    def test_multimeter_autorange_does_not_overload(self):
        self.power()
        self.dmm.send("CONF:VOLT:DC AUTO")
        self.assertEqual(self.dmm.number("READ?"), 5.0)

    def test_multimeter_ac_voltage_reads_the_ripple(self):
        self.power()
        self.siggen.send("FREQ 100;VOLT 1;OUTP ON")
        ripple = self.dmm.number("MEAS:VOLT:AC?")
        self.assertAlmostEqual(ripple, 10 ** (-69.9568 / 20) / (2 * 2 ** 0.5), places=7)

    def test_generator_offset_adds_to_the_input(self):
        self.power(volts=12.0)
        self.siggen.send("VOLT:OFFS 1;:OUTP ON")
        self.assertEqual(self.psu.number("MEAS:VOLT?"), 13.0)

    def test_scope_measures_the_rail_and_its_ripple(self):
        self.power()
        self.scope.send("CHAN1:SCAL 2;COUP DC")
        self.assertAlmostEqual(self.scope.number("MEAS:VAV? CHAN1"), 5.0, delta=2 * 8 / 256)
        self.siggen.send("FREQ 1000;VOLT 1;OUTP ON")
        self.scope.send("TIM:SCAL 1E-3;:CHAN2:SCAL 0.2;COUP AC")
        self.assertAlmostEqual(self.scope.number("MEAS:VPP? CHAN2"), 1.0, delta=0.02)
        self.assertAlmostEqual(self.scope.number("MEAS:VRMS? CHAN2"), 0.35355, delta=0.005)
        self.assertAlmostEqual(self.scope.number("MEAS:FREQ? CHAN2"), 1000.0, delta=20.0)

    def test_scope_overload_when_the_trace_leaves_the_screen(self):
        self.power()
        self.scope.send("CHAN1:SCAL 0.1;COUP DC")
        self.assertGreaterEqual(self.scope.number("MEAS:VRMS? CHAN1"), 9.9e37)
        self.scope.send("CHAN1:OFFS 5")
        self.assertLess(self.scope.number("MEAS:VRMS? CHAN1"), 9.9e37)

    def test_scope_frequency_undefined_on_too_few_cycles(self):
        self.power()
        self.siggen.send("FREQ 10;VOLT 1;OUTP ON")
        self.scope.send("TIM:SCAL 1E-3;:CHAN2:SCAL 0.2;COUP AC")
        self.assertGreaterEqual(self.scope.number("MEAS:FREQ? CHAN2"), 9.9e37)

    def test_scope_waveform_is_a_full_record(self):
        self.power()
        self.siggen.send("FUNC SQU;FREQ 1000;VOLT 1;OUTP ON")
        self.scope.send("WAV:SOUR CHAN2;:TIM:SCAL 1E-3;:CHAN2:SCAL 0.2;COUP AC")
        samples = [float(v) for v in self.scope.ask("WAV:DATA?").split(",")]
        self.assertEqual(len(samples), 1000)
        self.assertAlmostEqual(max(samples), 0.5, delta=0.01)
        self.assertAlmostEqual(min(samples), -0.5, delta=0.01)
        self.assertEqual(self.scope.number("WAV:XINC?"), 1e-5)


class NoiseAndSeeds(unittest.TestCase):
    def readings(self, seed, noise=1.0):
        with SimBench(seed=seed, noise=noise) as bench:
            psu = Client(bench.addresses["psu"])
            dmm = Client(bench.addresses["dmm"])
            psu.send("VOLT 12;OUTP ON")
            values = [dmm.number("READ?") for _ in range(20)]
            psu.close()
            dmm.close()
        return values

    def test_same_seed_repeats_exactly(self):
        self.assertEqual(self.readings(5), self.readings(5))

    def test_different_seed_differs(self):
        self.assertNotEqual(self.readings(5), self.readings(6))

    def test_noise_has_the_documented_size(self):
        values = self.readings(9)
        mean = sum(values) / len(values)
        rms = (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5
        self.assertAlmostEqual(mean, 5.0, delta=30e-6)
        self.assertGreater(rms, 10e-6)
        self.assertLess(rms, 60e-6)

    def test_noise_zero_is_exact(self):
        self.assertEqual(set(self.readings(5, noise=0.0)), {5.0})

    def test_more_integration_means_less_noise(self):
        with SimBench(seed=3) as bench:
            psu = Client(bench.addresses["psu"])
            dmm = Client(bench.addresses["dmm"])
            psu.send("VOLT 12;OUTP ON")

            def spread():
                values = [dmm.number("READ?") for _ in range(30)]
                return max(values) - min(values)

            dmm.send("VOLT:DC:NPLC 0.1")
            fast = spread()
            dmm.send("VOLT:DC:NPLC 100")
            slow = spread()
            psu.close()
            dmm.close()
        self.assertLess(slow, fast)


class Faults(unittest.TestCase):
    def with_fault(self, kind, after=1):
        bench = SimBench(noise=0.0, faults={"dmm": Fault(kind, after)}).start()
        self.addCleanup(bench.stop)
        client = Client(bench.addresses["dmm"], timeout_s=0.2)
        self.addCleanup(client.close)
        self.assertEqual(client.number("READ?"), 0.0)
        return bench, client

    def test_drop_closes_the_socket_and_refuses_new_sessions(self):
        bench, client = self.with_fault("drop")
        with self.assertRaisesRegex(Exception, "closed"):
            client.ask("READ?")
        fresh = Client(bench.addresses["dmm"], timeout_s=0.5)
        self.addCleanup(fresh.close)
        with self.assertRaisesRegex(Exception, "closed|lost"):
            fresh.ask("*IDN?")

    def test_hang_stops_answering_everything(self):
        _, client = self.with_fault("hang")
        with self.assertRaisesRegex(Exception, "no response"):
            client.ask("READ?")
        with self.assertRaisesRegex(Exception, "no response"):
            client.ask("*IDN?")

    def test_error_answers_and_queues_an_error(self):
        _, client = self.with_fault("error")
        self.assertEqual(client.number("READ?"), 0.0)
        self.assertIn("-300", client.ask("SYST:ERR?"))

    def test_garbage_response(self):
        _, client = self.with_fault("garbage")
        self.assertEqual(client.ask("READ?"), "#BAD#")

    def test_overload_response(self):
        _, client = self.with_fault("overload")
        self.assertGreaterEqual(client.number("READ?"), 9.9e37)

    def test_generator_has_no_readings_to_corrupt(self):
        self.assertEqual(parse_fault("siggen:drop:2")["siggen"], Fault("drop", 2))
        for bad in ("siggen:garbage", "siggen:overload:1"):
            with self.assertRaisesRegex(Exception, "no readings"):
                parse_fault(bad)

    def test_parse_fault(self):
        self.assertEqual(parse_fault("dmm:drop:5")["dmm"], Fault("drop", 5))
        self.assertEqual(parse_fault("psu:hang")["psu"], Fault("hang", 0))
        for bad in ("dmm", "dmm:melt", "laser:drop", "dmm:drop:x", "a:b:c:d"):
            with self.assertRaises(PlanError, msg=bad):
                parse_fault(bad)


class Lifecycle(unittest.TestCase):
    def test_binds_loopback_only(self):
        with SimBench() as bench:
            for address in bench.addresses.values():
                self.assertTrue(address.startswith("127.0.0.1:"), address)

    def test_ephemeral_ports_never_collide(self):
        with SimBench() as one, SimBench() as two:
            ports = list(one.addresses.values()) + list(two.addresses.values())
            self.assertEqual(len(set(ports)), 2 * len(one.devices))  # Installed driver plugins can add simulators.

    def test_consecutive_ports_from_a_base(self):
        for _ in range(20):
            base = random.randint(20000, 40000)
            try:
                bench = SimBench(base_port=base).start()
            except OSError:
                continue
            try:
                wanted = [f"127.0.0.1:{base + i}" for i in range(len(PORT_ORDER))]
                self.assertEqual([bench.addresses[k] for k in PORT_ORDER], wanted)
                return
            finally:
                bench.stop()
        self.skipTest("no free block of ports found")

    def test_stop_releases_ports(self):
        bench = SimBench().start()
        address = bench.addresses["dmm"]
        bench.stop()
        host, port = parse_address(address)
        time.sleep(0.01)
        with self.assertRaises(OSError):
            socket.create_connection((host, port), timeout=0.2).close()

    def test_unknown_unit_is_refused(self):
        with self.assertRaises(PlanError):
            SimBench(dut="nope")

    def test_bytes_arriving_in_pieces_and_batches(self):
        with SimBench(noise=0.0) as bench:
            host, port = parse_address(bench.addresses["psu"])
            with socket.create_connection((host, port), timeout=2.0) as raw:
                raw.sendall(b"*OP")
                time.sleep(0.02)
                raw.sendall(b"C?\r\n*TST?\n")
                buffer = b""
                while buffer.count(b"\n") < 2:
                    buffer += raw.recv(100)
        self.assertEqual(buffer, b"1\n0\n")

    def test_two_clients_at_once(self):
        with SimBench(noise=0.0) as bench:
            one = Client(bench.addresses["psu"])
            two = Client(bench.addresses["psu"])
            self.assertIn("SIM-PSU", one.ask("*IDN?"))
            self.assertIn("SIM-PSU", two.ask("*IDN?"))
            one.close()
            two.close()


if __name__ == "__main__":
    unittest.main()
