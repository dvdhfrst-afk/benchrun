"""Drivers: exact SCPI sent, safe parsing, and round trips against the simulators."""

import unittest

from benchrun.drivers import DRIVERS
from benchrun.drivers.base import Instrument, parse_error_line
from benchrun.drivers.dmm import Dmm
from benchrun.drivers.eload import ElectronicLoad
from benchrun.drivers.psu import PowerSupply
from benchrun.drivers.scope import Oscilloscope
from benchrun.drivers.siggen import SignalGenerator
from benchrun.errors import (
    BadResponse,
    ConnectionLost,
    ReadingOverload,
    ResponseTimeout,
    ScpiErrorReported,
)
from benchrun.sim.bench import SimBench
from benchrun.transport import TcpTransport, parse_address
from tests.support import FakeTransport


class ErrorLineParsing(unittest.TestCase):
    def test_forms_seen_in_the_wild(self):
        self.assertEqual(parse_error_line('+0,"No error"'), (0, "No error"))
        self.assertEqual(parse_error_line('-113,"Undefined header"'), (-113, "Undefined header"))
        self.assertEqual(parse_error_line('-222, "Data out of range"'), (-222, "Data out of range"))
        self.assertEqual(parse_error_line("0,No error"), (0, "No error"))

    def test_nonsense_is_none(self):
        self.assertIsNone(parse_error_line("ready"))


class BaseExchange(unittest.TestCase):
    def test_command_checks_the_error_queue(self):
        link = FakeTransport()
        Instrument(link, "x").write("FOO 1")
        self.assertEqual(link.sent, ["FOO 1", "SYST:ERR?"])

    def test_error_raises_with_instrument_command_and_all_errors(self):
        link = FakeTransport(errors=['-222,"Data out of range"', '-100,"Command error"'])
        with self.assertRaises(ScpiErrorReported) as caught:
            Instrument(link, "supply").write("VOLT 99")
        text = str(caught.exception)
        self.assertIn("supply", text)
        self.assertIn("VOLT 99", text)
        self.assertIn("-222", text)
        self.assertIn("-100", text)
        self.assertEqual(len(caught.exception.errors), 2)

    def test_reading_also_checks_the_error_queue(self):
        link = FakeTransport(replies=["1.5"], errors=['-300,"Device-specific error"'])
        with self.assertRaises(ScpiErrorReported):
            Instrument(link, "x").query_number("FOO?")

    def test_query_number(self):
        link = FakeTransport(replies=["+1.5E+00"])
        self.assertEqual(Instrument(link, "x").query_number("FOO?"), 1.5)

    def test_garbage_is_a_bad_response(self):
        link = FakeTransport(replies=["#BAD#"])
        with self.assertRaises(BadResponse):
            Instrument(link, "x").query_number("FOO?")

    def test_nan_and_infinity_are_bad_responses(self):
        for text in ("NaN", "nan", "inf", "-Inf"):
            link = FakeTransport(replies=[text])
            with self.assertRaises(BadResponse, msg=text):
                Instrument(link, "x").query_number("FOO?")

    def test_overload_is_recognized_either_sign(self):
        for text in ("9.9E37", "+9.900000E+37", "-9.9E37", "9.91E37"):
            link = FakeTransport(replies=[text])
            with self.assertRaises(ReadingOverload, msg=text):
                Instrument(link, "x").query_number("FOO?")

    def test_stale_data_refuses_to_continue(self):
        link = FakeTransport(replies=["1.0"])
        link.stale = "9.99\n"
        with self.assertRaisesRegex(BadResponse, "unexpected data"):
            Instrument(link, "x").query_number("FOO?")

    def test_error_queue_garbage_is_a_bad_response(self):
        link = FakeTransport(errors=["whatever"])
        with self.assertRaises(BadResponse):
            Instrument(link, "x").write("FOO")

    def test_endless_error_queue_cannot_hang(self):
        link = FakeTransport(errors=['-100,"Command error"'] * 500)
        with self.assertRaises(ScpiErrorReported) as caught:
            Instrument(link, "x").write("FOO")
        self.assertEqual(len(caught.exception.errors), 32)

    def test_reset_clears_then_resets(self):
        link = FakeTransport()
        Instrument(link, "x").reset()
        self.assertEqual(link.commands, ["*CLS", "*RST"])

    def test_identify_strips_whitespace(self):
        link = FakeTransport(replies=["Maker,Model,1,1 "])
        self.assertEqual(Instrument(link, "x").identify(), "Maker,Model,1,1")

    def test_fault_is_tagged_with_the_instrument_name(self):
        class Dead(FakeTransport):
            def write_line(self, text):
                raise ConnectionLost("gone")

        with self.assertRaises(ConnectionLost) as caught:
            Instrument(Dead(), "dmm").query_number("FOO?")
        self.assertEqual(str(caught.exception), "dmm: gone")

    def test_timeout_message_includes_error_queue_context(self):
        class Silent(FakeTransport):
            def __init__(self):
                super().__init__(replies=[""], errors=['-113,"Undefined header"'])
                self.first = True

            def read_line(self):
                if self.first:
                    self.first = False
                    self.pending.clear()
                    raise ResponseTimeout("no response within 1 s")
                return super().read_line()

        with self.assertRaises(ResponseTimeout) as caught:
            Instrument(Silent(), "dmm").query_number("BOGUS?")
        text = str(caught.exception)
        self.assertIn("BOGUS?", text)
        self.assertIn("-113", text)

    def test_commands_can_be_overridden(self):
        link = FakeTransport(replies=["5"])
        psu = PowerSupply(link, "x", {"measure_voltage": "MEASURE:VOLTAGE:DC?"})
        self.assertEqual(psu.measure_voltage(), 5.0)
        self.assertEqual(link.commands, ["MEASURE:VOLTAGE:DC?"])

    def test_override_problems_are_explained(self):
        self.assertIsNone(PowerSupply.override_problem("set_voltage", "APPL {volts}"))
        self.assertIn("unknown command key", PowerSupply.override_problem("nope", "X"))
        self.assertIn("unknown fields", PowerSupply.override_problem("set_voltage", "APPL {amps}"))
        self.assertIn("bad template", PowerSupply.override_problem("set_voltage", "APPL {volts"))

    def test_override_applies_only_to_that_instance(self):
        PowerSupply(FakeTransport(), "x", {"output": "FOO {state}"})
        self.assertEqual(PowerSupply.COMMANDS["output"], "OUTP {state}")


class ExactCommands(unittest.TestCase):
    def test_dmm_configures_once_then_reads(self):
        link = FakeTransport(replies=["5.0", "5.1"])
        dmm = Dmm(link, "d")
        self.assertEqual(dmm.measure_dc_voltage(range_v=10.0, nplc=1.0), 5.0)
        self.assertEqual(dmm.measure_dc_voltage(range_v=10.0, nplc=1.0), 5.1)
        self.assertEqual(link.commands, ["CONF:VOLT:DC 10", "VOLT:DC:NPLC 1", "READ?", "READ?"])

    def test_dmm_autorange_and_change_of_function_reconfigures(self):
        link = FakeTransport(replies=["5.0", "0.1", "0.2"])
        dmm = Dmm(link, "d")
        dmm.measure_dc_voltage()
        dmm.measure_dc_current(range_a=1.0)
        dmm.measure_ac_voltage()
        self.assertEqual(
            link.commands,
            ["CONF:VOLT:DC AUTO", "READ?", "CONF:CURR:DC 1", "READ?", "CONF:VOLT:AC AUTO", "READ?"],
        )

    def test_dmm_reset_forgets_configuration(self):
        link = FakeTransport(replies=["5.0", "5.0"])
        dmm = Dmm(link, "d")
        dmm.measure_dc_voltage()
        dmm.reset()
        dmm.measure_dc_voltage()
        self.assertEqual(link.commands.count("CONF:VOLT:DC AUTO"), 2)

    def test_dmm_failed_configuration_is_not_cached(self):
        link = FakeTransport(replies=["5.0"], errors=['-222,"Data out of range"'])
        dmm = Dmm(link, "d")
        with self.assertRaises(ScpiErrorReported):
            dmm.measure_dc_voltage(range_v=1e9)
        dmm.measure_dc_voltage(range_v=1e9)
        self.assertEqual(link.commands.count("CONF:VOLT:DC 1000000000"), 2)

    def test_supply(self):
        link = FakeTransport(replies=["12", "0.5", "1024"])
        psu = PowerSupply(link, "p")
        psu.set_voltage(12.5)
        psu.set_current_limit(0.25)
        psu.output_on()
        psu.output_off()
        self.assertEqual(psu.measure_voltage(), 12.0)
        self.assertEqual(psu.measure_current(), 0.5)
        self.assertEqual(psu.current_limited(), 1.0)
        self.assertEqual(
            link.commands,
            ["VOLT 12.5", "CURR 0.25", "OUTP ON", "OUTP OFF", "MEAS:VOLT?", "MEAS:CURR?", "STAT:OPER:COND?"],
        )

    def test_precise_values_are_not_rounded_away(self):
        link = FakeTransport()
        PowerSupply(link, "p").set_voltage(12.3456789)
        self.assertEqual(link.commands, ["VOLT 12.3456789"])

    def test_load(self):
        link = FakeTransport(replies=["5", "1"])
        load = ElectronicLoad(link, "l")
        load.set_current(1)
        load.input_on()
        load.measure_voltage()
        load.measure_current()
        load.input_off()
        self.assertEqual(link.commands, ["CURR 1", "INP ON", "MEAS:VOLT?", "MEAS:CURR?", "INP OFF"])

    def test_scope(self):
        link = FakeTransport(replies=["1", "2", "3", "4"])
        scope = Oscilloscope(link, "s")
        scope.setup_channel(2, 0.5, offset_v=1.0, coupling="AC")
        scope.set_timebase(1e-3)
        scope.measure_vpp(2)
        scope.measure_vrms(1)
        scope.measure_vavg(1.0)
        scope.measure_frequency()
        self.assertEqual(
            link.commands,
            [
                "CHAN2:COUP AC",
                "CHAN2:SCAL 0.5",
                "CHAN2:OFFS 1",
                "TIM:SCAL 0.001",
                "MEAS:VPP? CHAN2",
                "MEAS:VRMS? CHAN1",
                "MEAS:VAV? CHAN1",
                "MEAS:FREQ? CHAN1",
            ],
        )

    def test_scope_waveform_garbage(self):
        link = FakeTransport(replies=["1E-6", "1,2,nope"])
        with self.assertRaises(BadResponse):
            Oscilloscope(link, "s").fetch_waveform()

    def test_generator(self):
        link = FakeTransport()
        gen = SignalGenerator(link, "g")
        gen.set_waveform("SQU", 1000, 2, 0.5)
        gen.set_frequency(50)
        gen.set_amplitude(1)
        gen.output_on()
        gen.output_off()
        self.assertEqual(
            link.commands,
            ["FUNC SQU", "FREQ 1000", "VOLT 2", "VOLT:OFFS 0.5", "FREQ 50", "VOLT 1", "OUTP ON", "OUTP OFF"],
        )

    def test_safe_state_turns_sources_off(self):
        for driver, command in ((PowerSupply, "OUTP OFF"), (ElectronicLoad, "INP OFF"), (SignalGenerator, "OUTP OFF")):
            link = FakeTransport()
            driver(link, "x").safe_state()
            self.assertEqual(link.commands, [command])

    def test_safe_state_is_nothing_for_passive_instruments(self):
        for driver in (Dmm, Oscilloscope, Instrument):
            link = FakeTransport()
            driver(link, "x").safe_state()
            self.assertEqual(link.commands, [])


class RawCommandsAndRememberedSettings(unittest.TestCase):
    def test_multimeter_reconfigures_after_a_raw_write(self):
        link = FakeTransport(["5.0", "5.0"])
        meter = Dmm(link)
        meter.measure_dc_voltage()
        meter.write("CONF:CURR:DC")
        meter.measure_dc_voltage()
        self.assertEqual(link.commands.count("CONF:VOLT:DC AUTO"), 2)

    def test_multimeter_reconfigures_after_a_raw_query(self):
        link = FakeTransport(["5.0", "0.1", "5.0"])
        meter = Dmm(link)
        meter.measure_dc_voltage()
        meter.query_number("MEAS:CURR:DC?")
        meter.measure_dc_voltage()
        self.assertEqual(link.commands.count("CONF:VOLT:DC AUTO"), 2)

    def test_multimeter_still_skips_repeat_configuration(self):
        link = FakeTransport(["5.0", "5.0"])
        meter = Dmm(link)
        meter.measure_dc_voltage()
        meter.measure_dc_voltage()
        self.assertEqual(link.commands.count("CONF:VOLT:DC AUTO"), 1)


class ConstantCurrentBit(unittest.TestCase):
    def test_default_is_bit_10(self):
        self.assertEqual(PowerSupply(FakeTransport(["1024"])).current_limited(), 1.0)
        self.assertEqual(PowerSupply(FakeTransport(["256"])).current_limited(), 0.0)

    def test_plan_can_name_another_bit(self):
        supply = PowerSupply(FakeTransport(["256"]), commands={"constant_current_bit": "8"})
        self.assertEqual(supply.current_limited(), 1.0)

    def test_bad_bit_is_refused(self):
        for bad in ("x", "32", "-1", ""):
            self.assertIn("bit number", PowerSupply.override_problem("constant_current_bit", bad))
        self.assertIsNone(PowerSupply.override_problem("constant_current_bit", "8"))
        self.assertIsNone(PowerSupply.override_problem("output", "OUTP:STAT {state}"))


class Registry(unittest.TestCase):
    def test_registered_drivers(self):
        self.assertEqual(set(DRIVERS), {"dmm", "psu", "eload", "scope", "siggen", "scpi"})

    def test_every_reading_declares_a_unit_attribute(self):
        for driver in DRIVERS.values():
            for func in driver.readings().values():
                self.assertTrue(hasattr(func, "_bench_unit"))

    def test_all_drivers_expose_raw_access(self):
        for driver in DRIVERS.values():
            self.assertIn("write", driver.actions())
            self.assertIn("query_number", driver.readings())

    def test_bind_problem(self):
        func = PowerSupply.actions()["set_voltage"]
        self.assertIsNone(PowerSupply.bind_problem(func, {"volts": 1}))
        self.assertIsNotNone(PowerSupply.bind_problem(func, {"voltage": 1}))
        self.assertIsNotNone(PowerSupply.bind_problem(func, {}))


class AgainstSimulators(unittest.TestCase):
    def setUp(self):
        self.bench = SimBench(noise=0.0).start()
        self.addCleanup(self.bench.stop)

    def connect(self, cls, kind):
        host, port = parse_address(self.bench.addresses[kind])
        driver = cls(TcpTransport(host, port, 1.0), kind)
        driver.connect()
        self.addCleanup(driver.close)
        driver.reset()
        return driver

    def test_full_bench_with_real_drivers(self):
        psu = self.connect(PowerSupply, "psu")
        load = self.connect(ElectronicLoad, "eload")
        dmm = self.connect(Dmm, "dmm")
        psu.set_current_limit(3)
        psu.set_voltage(12)
        psu.output_on()
        load.set_current(0.5)
        load.input_on()
        self.assertAlmostEqual(dmm.measure_dc_voltage(range_v=10, nplc=1), 4.9925, places=8)
        self.assertAlmostEqual(dmm.measure_dc_current(range_a=1), 0.5, places=8)
        self.assertAlmostEqual(psu.measure_current(), 0.503, places=8)
        self.assertEqual(load.measure_voltage(), 4.9925)
        self.assertEqual(psu.current_limited(), 0.0)

    def test_scope_and_generator(self):
        psu = self.connect(PowerSupply, "psu")
        gen = self.connect(SignalGenerator, "siggen")
        scope = self.connect(Oscilloscope, "scope")
        psu.set_voltage(12)
        psu.output_on()
        gen.set_waveform("SIN", 1000, 1.0)
        gen.output_on()
        scope.setup_channel(2, 0.2, coupling="AC")
        scope.set_timebase(1e-3)
        self.assertAlmostEqual(scope.measure_vrms(2), 0.35355, delta=0.005)
        self.assertAlmostEqual(scope.measure_vpp(2), 1.0, delta=0.02)
        self.assertAlmostEqual(scope.measure_frequency(2), 1000.0, delta=20.0)
        increment, samples = scope.fetch_waveform(2)
        self.assertEqual(len(samples), 1000)
        self.assertEqual(increment, 1e-5)

    def test_rejected_setting_raises_at_once(self):
        psu = self.connect(PowerSupply, "psu")
        with self.assertRaisesRegex(ScpiErrorReported, "-222"):
            psu.set_voltage(99)

    def test_unknown_query_times_out_with_the_real_reason(self):
        dmm = self.connect(Dmm, "dmm")
        with self.assertRaisesRegex(ResponseTimeout, "-113"):
            dmm.query_number("FAKE:QUERY?")

    def test_overload_from_a_too_small_range(self):
        psu = self.connect(PowerSupply, "psu")
        dmm = self.connect(Dmm, "dmm")
        psu.set_voltage(12)
        psu.output_on()
        with self.assertRaises(ReadingOverload):
            dmm.measure_dc_voltage(range_v=1.0)


if __name__ == "__main__":
    unittest.main()
