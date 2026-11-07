"""The source measure unit, counter, switch, LCR meter and multi-output supply."""

import unittest

from benchrun.drivers.counter import FrequencyCounter
from benchrun.drivers.dmm import Dmm
from benchrun.drivers.lcr import LcrMeter
from benchrun.drivers.psu import PowerSupply
from benchrun.drivers.psu_multi import MultiOutputSupply
from benchrun.drivers.siggen import SignalGenerator
from benchrun.drivers.smu import SourceMeasureUnit
from benchrun.drivers.switch import Switch
from benchrun.errors import BadResponse, CommandRefused, InstrumentFault, ReadingOverload
from benchrun.sim.bench import SimBench
from benchrun.transport import TcpTransport, parse_address
from tests.support import FakeTransport


class ExactCommands(unittest.TestCase):
    def test_source_measure_unit_voltage(self):
        link = FakeTransport(["1.5E-03", "5.0"])
        smu = SourceMeasureUnit(link, "x")
        smu.source_voltage(5.0, 0.01)
        smu.output_on()
        self.assertEqual(smu.measure_current(), 1.5e-3)
        self.assertEqual(smu.measure_voltage(), 5.0)
        self.assertEqual(
            link.commands,
            [
                ":SOUR:FUNC VOLT",
                ":SENS:CURR:PROT 0.01",
                ":SOUR:VOLT:LEV 5",
                ":OUTP ON",
                ':SENS:FUNC "CURR"',
                ":FORM:ELEM CURR",
                ":READ?",
                ':SENS:FUNC "VOLT"',
                ":FORM:ELEM VOLT",
                ":READ?",
            ],
        )

    def test_source_measure_unit_current(self):
        link = FakeTransport(["0"])
        smu = SourceMeasureUnit(link, "x")
        smu.source_current(0.001, 20.0)
        self.assertEqual(smu.at_limit(), 0.0)
        self.assertEqual(
            link.commands,
            [":SOUR:FUNC CURR", ":SENS:VOLT:PROT 20", ":SOUR:CURR:LEV 0.001", ":SENS:VOLT:PROT:TRIP?"],
        )

    def test_source_measure_unit_limit_needs_a_source(self):
        with self.assertRaises(CommandRefused):
            SourceMeasureUnit(FakeTransport(), "x").at_limit()

    def test_counter_defaults(self):
        link = FakeTransport(["1000.0", "1000.0"])
        counter = FrequencyCounter(link, "x")
        self.assertEqual(counter.measure_frequency(), 1000.0)
        counter.measure_frequency()
        self.assertEqual(link.commands, ["CONF:FREQ DEF,DEF,(@1)", "READ?", "READ?"])

    def test_counter_puts_the_gate_time_back_after_configuring(self):
        link = FakeTransport(["1.0"])
        counter = FrequencyCounter(link, "x")
        counter.set_gate_time(1.0)
        counter.measure_frequency(channel=2, expected_hz=1e6, resolution_hz=0.1)
        self.assertEqual(
            link.commands,
            ["SENS:FREQ:GATE:TIME 1", "CONF:FREQ 1000000,0.1,(@2)", "SENS:FREQ:GATE:TIME 1", "READ?"],
        )

    def test_counter_channel_must_be_whole(self):
        with self.assertRaises(CommandRefused):
            FrequencyCounter(FakeTransport(), "x").measure_frequency(channel=1.5)

    def test_switch_opens_what_it_closed_and_checks(self):
        link = FakeTransport(["0", "0"])
        switch = Switch(link, "x")
        switch.close_channel(101)
        switch.close_channel(105)
        switch.safe_state()
        self.assertEqual(
            link.commands,
            [
                "ROUT:CLOS (@101)",
                "ROUT:CLOS (@105)",
                "ROUT:OPEN (@101)",
                "ROUT:CLOS? (@101)",
                "ROUT:OPEN (@105)",
                "ROUT:CLOS? (@105)",
            ],
        )

    def test_switch_with_nothing_closed_does_nothing(self):
        link = FakeTransport()
        Switch(link, "x").safe_state()
        self.assertEqual(link.commands, [])

    def test_switch_channel_opened_by_the_plan_is_forgotten(self):
        link = FakeTransport()
        switch = Switch(link, "x")
        switch.close_channel(101)
        switch.open_channel(101)
        switch.safe_state()
        self.assertEqual(link.commands, ["ROUT:CLOS (@101)", "ROUT:OPEN (@101)"])

    def test_switch_close_only_leaves_only_that_channel_to_open(self):
        link = FakeTransport(["0"])
        switch = Switch(link, "x")
        switch.close_channel(101)
        switch.close_only(102)
        switch.safe_state()
        self.assertEqual(link.commands[-2:], ["ROUT:OPEN (@102)", "ROUT:CLOS? (@102)"])
        self.assertEqual(len(link.commands), 4)

    def test_switch_that_stays_closed_is_reported(self):
        switch = Switch(FakeTransport(["1"]), "x")
        switch.close_channel(101)
        with self.assertRaisesRegex(InstrumentFault, "101 still reads closed"):
            switch.safe_state()

    def test_switch_with_an_unreadable_state_is_reported(self):
        switch = Switch(FakeTransport(["maybe"]), "x")
        switch.close_channel(101)
        with self.assertRaises(BadResponse):
            switch.safe_state()

    def test_switch_open_all_override(self):
        link = FakeTransport()
        Switch(link, "x", {"open_all": "ROUT:OPEN (@101:120)"}).safe_state()
        self.assertEqual(link.commands, ["ROUT:OPEN (@101:120)"])

    def test_lcr_exact_sequence(self):
        link = FakeTransport(["+1.00000E-05,+2.00000E-02,+0"])
        meter = LcrMeter(link, "x")
        meter.configure("CPD", 1000.0, 1.0)
        self.assertEqual(meter.measure_capacitance(), 1e-5)
        self.assertEqual(
            link.commands,
            ["FUNC:IMP CPD", "FREQ 1000", "VOLT 1", "TRIG:SOUR BUS", "INIT", "TRIG:IMM", "FETC?"],
        )

    def test_lcr_secondary_and_fourth_field(self):
        meter = LcrMeter(FakeTransport(["+1.0E-05,+2.0E-02,+0,+3"]), "x")
        meter.configure("CPD", 1000.0, 1.0)
        self.assertEqual(meter.measure_secondary(), 2e-2)

    def test_lcr_refuses_a_value_that_would_mean_something_else(self):
        meter = LcrMeter(FakeTransport(), "x")
        with self.assertRaisesRegex(CommandRefused, "needs configure"):
            meter.measure_capacitance()
        meter.configure("LSQ", 1000.0, 1.0)
        with self.assertRaisesRegex(CommandRefused, "cannot come from"):
            meter.measure_capacitance()
        with self.assertRaisesRegex(CommandRefused, "cannot come from"):
            meter.measure_impedance()

    def test_lcr_primary_of_a_function_with_no_named_reading(self):
        meter = LcrMeter(FakeTransport(["+1.0E-03,+2.0E-03,+0"]), "x")
        meter.configure("GB", 1000.0, 1.0)
        self.assertEqual(meter.measure_primary(), 1e-3)

    def test_lcr_refuses_an_unknown_function(self):
        link = FakeTransport()
        with self.assertRaisesRegex(CommandRefused, "unknown LCR function"):
            LcrMeter(link, "x").configure("CPD; *RST", 1000.0, 1.0)
        self.assertEqual(link.commands, [])

    def test_lcr_status_fields(self):
        cases = (
            ("+1,+1,+1", ReadingOverload),
            ("+1,+1,+3", BadResponse),
            ("+1,+1,+4", BadResponse),
            ("+1,+1,-1", BadResponse),
            ("+1,+1,+9", BadResponse),
            ("+1,+1", BadResponse),
            ("nonsense", BadResponse),
            ("x,+1,+0", BadResponse),
            ("+1,+1,x", BadResponse),
        )
        for reply, error in cases:
            meter = LcrMeter(FakeTransport([reply]), "x")
            meter.configure("CPD", 1000.0, 1.0)
            with self.assertRaises(error, msg=reply):
                meter.measure_capacitance()

    def test_multi_supply_exact_commands(self):
        link = FakeTransport(["3.3", "0.1", "2", "0", "0"])
        supply = MultiOutputSupply(link, "x")
        supply.set_voltage(2, 3.3)
        supply.set_current_limit(2, 0.5)
        supply.output_on(2)
        supply.output_on(1)
        self.assertEqual(supply.measure_voltage(2), 3.3)
        self.assertEqual(supply.measure_current(2), 0.1)
        self.assertEqual(supply.current_limited(2), 0.0)
        supply.safe_state()
        self.assertEqual(
            link.commands,
            [
                "VOLT 3.3,(@2)",
                "CURR 0.5,(@2)",
                "OUTP ON,(@2)",
                "OUTP ON,(@1)",
                "MEAS:VOLT? (@2)",
                "MEAS:CURR? (@2)",
                "STAT:QUES:INST:ISUM2:COND?",
                "OUTP OFF,(@1)",
                "OUTP? (@1)",
                "OUTP OFF,(@2)",
                "OUTP? (@2)",
            ],
        )

    def test_multi_supply_keeps_going_when_one_output_stays_on(self):
        link = FakeTransport(["1", "0"])
        supply = MultiOutputSupply(link, "x")
        supply.output_on(1)
        supply.output_on(2)
        with self.assertRaisesRegex(InstrumentFault, "output 1"):
            supply.safe_state()
        self.assertIn("OUTP OFF,(@2)", link.commands)

    def test_multi_supply_all_off_override(self):
        link = FakeTransport()
        MultiOutputSupply(link, "x", {"all_off": "OUTP OFF,(@1:3)"}).safe_state()
        self.assertEqual(link.commands, ["OUTP OFF,(@1:3)"])

    def test_multi_supply_modes(self):
        supply = MultiOutputSupply(FakeTransport(["1", "3"]), "x")
        self.assertEqual(supply.current_limited(1), 1.0)
        with self.assertRaisesRegex(InstrumentFault, "hardware failure"):
            supply.current_limited(1)

    def test_multimeter_additions(self):
        link = FakeTransport(["10000.2", "10000.0", "0.001", "1000.0"])
        meter = Dmm(link, "x")
        self.assertEqual(meter.measure_resistance(), 10000.2)
        self.assertEqual(meter.measure_resistance_4w(100000.0), 10000.0)
        self.assertEqual(meter.measure_ac_current(1.0), 0.001)
        self.assertEqual(meter.measure_frequency(), 1000.0)
        self.assertEqual(
            link.commands,
            ["CONF:RES DEF", "READ?", "CONF:FRES 100000", "READ?", "CONF:CURR:AC 1", "READ?", "CONF:FREQ", "READ?"],
        )


class AgainstSimulators(unittest.TestCase):
    def setUp(self):
        self.bench = SimBench(seed=5).start()
        self.addCleanup(self.bench.stop)
        self.links = []

    def connect(self, driver, kind):
        host, port = parse_address(self.bench.addresses[kind])
        link = TcpTransport(host, port, timeout_s=2.0)
        self.addCleanup(link.close)
        link.open()
        return driver(link, kind)

    def test_source_measure_unit_follows_ohms_law_and_its_limit(self):
        smu = self.connect(SourceMeasureUnit, "smu")
        smu.source_voltage(5.0, 0.1)
        smu.output_on()
        self.assertAlmostEqual(smu.measure_current(), 5.0 / 10000.0, places=6)
        self.assertEqual(smu.at_limit(), 0.0)
        smu.source_voltage(5.0, 1e-4)
        self.assertAlmostEqual(smu.measure_current(), 1e-4, places=6)
        self.assertAlmostEqual(smu.measure_voltage(), 1.0, places=2)
        self.assertEqual(smu.at_limit(), 1.0)
        smu.safe_state()

    def test_source_measure_unit_current_mode(self):
        smu = self.connect(SourceMeasureUnit, "smu")
        smu.source_current(1e-3, 20.0)
        smu.output_on()
        self.assertAlmostEqual(smu.measure_voltage(), 10.0, places=2)
        smu.source_current(1e-3, 2.0)
        self.assertAlmostEqual(smu.measure_voltage(), 2.0, places=2)
        self.assertEqual(smu.at_limit(), 1.0)
        smu.safe_state()

    def test_counter_reads_the_generator(self):
        generator = self.connect(SignalGenerator, "siggen")
        generator.set_waveform("SIN", 1234.5, 0.1)
        generator.output_on()
        counter = self.connect(FrequencyCounter, "counter")
        counter.set_gate_time(1.0)
        self.assertAlmostEqual(counter.measure_frequency(), 1234.5, places=2)

    def test_counter_with_nothing_to_count_is_an_overload(self):
        with self.assertRaises(ReadingOverload):
            self.connect(FrequencyCounter, "counter").measure_frequency()

    def test_switch_routes_the_multimeter(self):
        supply = self.connect(PowerSupply, "psu")
        supply.set_current_limit(1.0)
        supply.set_voltage(12.0)
        supply.output_on()
        switch = self.connect(Switch, "switch")
        meter = self.connect(Dmm, "dmm")
        direct = meter.measure_dc_voltage()
        self.assertAlmostEqual(direct, 5.0, places=0)
        switch.close_only(102)
        self.assertEqual(switch.is_closed(102), 1.0)
        self.assertEqual(switch.is_closed(101), 0.0)
        through_vin = meter.measure_dc_voltage()
        switch.close_only(101)
        through_vout = meter.measure_dc_voltage()
        self.assertAlmostEqual(direct, through_vout, places=2)
        self.assertAlmostEqual(through_vin, 12.0, places=0)
        switch.safe_state()
        self.assertEqual(switch.is_closed(101), 0.0)
        self.assertAlmostEqual(meter.measure_dc_voltage(), 0.0, places=2)

    def test_switch_refuses_a_channel_that_is_not_there(self):
        switch = self.connect(Switch, "switch")
        with self.assertRaises(InstrumentFault):
            switch.close_channel(999)

    def test_lcr_reads_the_capacitor_each_way(self):
        meter = self.connect(LcrMeter, "lcr")
        meter.configure("CPD", 1000.0, 1.0)
        self.assertAlmostEqual(meter.measure_capacitance() * 1e6, 10.0, places=1)
        self.assertAlmostEqual(meter.measure_secondary(), 0.02, places=3)
        meter.configure("ZTD", 1000.0, 1.0)
        self.assertAlmostEqual(meter.measure_impedance(), 15.9, places=0)
        meter.configure("LPD", 1000.0, 1.0)
        self.assertLess(meter.measure_inductance(), 0.0)

    def test_multi_supply_outputs_are_separate(self):
        supply = self.connect(MultiOutputSupply, "psu_multi")
        supply.set_voltage(1, 5.0)
        supply.set_current_limit(1, 1.0)
        supply.set_voltage(2, 12.0)
        supply.set_current_limit(2, 0.05)
        supply.output_on(1)
        supply.output_on(2)
        self.assertAlmostEqual(supply.measure_voltage(1), 5.0, places=2)
        self.assertAlmostEqual(supply.measure_current(1), 0.05, places=2)
        self.assertEqual(supply.current_limited(1), 0.0)
        self.assertAlmostEqual(supply.measure_voltage(2), 5.0, places=2)
        self.assertEqual(supply.current_limited(2), 1.0)
        self.assertAlmostEqual(supply.measure_voltage(3), 0.0, places=2)
        supply.safe_state()
        self.assertAlmostEqual(supply.measure_voltage(1), 0.0, places=2)

    def test_multi_supply_refuses_an_output_that_is_not_there(self):
        supply = self.connect(MultiOutputSupply, "psu_multi")
        with self.assertRaises(InstrumentFault):
            supply.set_voltage(9, 1.0)

    def test_multimeter_resistance_and_frequency(self):
        meter = self.connect(Dmm, "dmm")
        self.assertAlmostEqual(meter.measure_resistance(), 10000.2, places=1)
        self.assertAlmostEqual(meter.measure_resistance_4w(), 10000.0, places=1)
        self.assertEqual(meter.measure_frequency(), 0.0)


if __name__ == "__main__":
    unittest.main()
