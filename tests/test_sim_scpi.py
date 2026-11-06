"""The simulators' SCPI parser, tested on the device object without sockets."""

import unittest

from benchrun.sim.instruments import SimDmm, SimGenerator, SimScope, SimSupply
from benchrun.sim.model import BenchModel, RegulatorParams
from benchrun.sim.scpi import ERROR_QUEUE_SIZE, parse_number, ScpiError


def supply():
    return SimSupply(BenchModel(RegulatorParams()), noise=0.0)


def next_error(device):
    return device.process("SYST:ERR?")


class CommonCommands(unittest.TestCase):
    def test_idn(self):
        self.assertEqual(supply().process("*IDN?"), "benchrun,SIM-PSU,SIM0002,1.0")

    def test_common_commands_are_case_insensitive(self):
        self.assertEqual(supply().process("*idn?"), "benchrun,SIM-PSU,SIM0002,1.0")

    def test_rst_restores_defaults(self):
        device = supply()
        device.process("VOLT 12;OUTP ON")
        device.process("*RST")
        self.assertEqual(device.process("OUTP?"), "0")
        self.assertEqual(float(device.process("VOLT?")), 0.0)

    def test_cls_clears_queue_and_event_register(self):
        device = supply()
        device.process("NOPE")
        device.process("*CLS")
        self.assertEqual(next_error(device), '+0,"No error"')
        self.assertEqual(device.process("*ESR?"), "0")

    def test_opc_tst_and_version(self):
        device = supply()
        self.assertEqual(device.process("*OPC?"), "1")
        self.assertEqual(device.process("*TST?"), "0")
        self.assertEqual(device.process("SYST:VERS?"), "1999.0")

    def test_esr_reports_command_error_once(self):
        device = supply()
        device.process("NOPE")
        self.assertEqual(int(device.process("*ESR?")) & 0x20, 0x20)
        self.assertEqual(device.process("*ESR?"), "0")

    def test_esr_reports_execution_error(self):
        device = supply()
        device.process("VOLT 99")
        self.assertEqual(int(device.process("*ESR?")) & 0x10, 0x10)

    def test_stb_shows_pending_error(self):
        device = supply()
        self.assertEqual(int(device.process("*STB?")) & 0x04, 0)
        device.process("NOPE")
        self.assertEqual(int(device.process("*STB?")) & 0x04, 0x04)

    def test_ese_and_sre_round_trip(self):
        device = supply()
        device.process("*ESE 32;*SRE 16")
        self.assertEqual(device.process("*ESE?;*SRE?"), "32;16")


class Headers(unittest.TestCase):
    def test_long_and_short_forms_mixed_case(self):
        for text in ("VOLT 3", "VOLTAGE 3", "volt 3", "Voltage 3", "SOUR:VOLT 3", "SOURCE:VOLTAGE:LEVEL 3"):
            device = supply()
            device.process(text)
            self.assertEqual(next_error(device), '+0,"No error"', text)
            self.assertEqual(float(device.process("VOLT?")), 3.0, text)

    def test_partial_mnemonic_is_rejected(self):
        device = supply()
        device.process("VOLTA 3")
        self.assertIn("-113", next_error(device))

    def test_optional_nodes_in_the_middle(self):
        device = supply()
        device.process("SOUR:VOLT:LEV:IMM:AMPL 7")
        self.assertEqual(float(device.process("SOUR:VOLT:LEV:IMM?")), 7.0)

    def test_compound_message_uses_relative_path(self):
        device = supply()
        device.process("SOUR:VOLT 5;CURR 0.5")
        self.assertEqual(next_error(device), '+0,"No error"')
        self.assertEqual(float(device.process("VOLT?")), 5.0)
        self.assertEqual(float(device.process("CURR?")), 0.5)

    def test_relative_path_applies_only_below_the_last_header(self):
        device = supply()
        # After VOLT:LEV the path is VOLT, so a bare AMPL resolves under it.
        device.process("VOLT:LEV 5;AMPL 6")
        self.assertEqual(next_error(device), '+0,"No error"')
        self.assertEqual(float(device.process("VOLT?")), 6.0)

    def test_leading_colon_resets_the_path(self):
        device = supply()
        device.process("SOUR:VOLT 5;:OUTP ON")
        self.assertEqual(next_error(device), '+0,"No error"')
        self.assertEqual(device.process("OUTP?"), "1")

    def test_path_does_not_survive_into_the_next_message(self):
        device = supply()
        device.process("OUTP:STAT ON")
        device.process("STAT OFF")
        self.assertIn("-113", next_error(device))

    def test_common_command_does_not_change_the_path(self):
        device = supply()
        device.process("SOUR:VOLT 5;*CLS;CURR 0.5")
        self.assertEqual(next_error(device), '+0,"No error"')
        self.assertEqual(float(device.process("CURR?")), 0.5)

    def test_compound_queries_join_with_semicolon(self):
        self.assertEqual(supply().process("*OPC?;*TST?"), "1;0")

    def test_numeric_suffix_selects_channel(self):
        scope = SimScope(BenchModel(RegulatorParams()), noise=0.0)
        scope.process("CHAN2:SCAL 0.5;:CHANNEL1:SCALE 2")
        self.assertEqual(float(scope.process("CHAN2:SCAL?")), 0.5)
        self.assertEqual(float(scope.process("CHAN1:SCAL?")), 2.0)
        self.assertEqual(next_error(scope), '+0,"No error"')

    def test_channel_out_of_range(self):
        scope = SimScope(BenchModel(RegulatorParams()), noise=0.0)
        scope.process("CHAN3:SCAL 1")
        self.assertIn("-114", next_error(scope))

    def test_query_with_bad_header_returns_nothing(self):
        device = supply()
        self.assertIsNone(device.process("MEAS:NOPE?"))
        self.assertIn("-113", next_error(device))

    def test_command_form_of_a_query_only_header(self):
        device = supply()
        device.process("MEAS:VOLT")
        self.assertIn("-113", next_error(device))

    def test_syntax_errors(self):
        for text in ("VOLT::LEV 3", "1VOLT 3", "VOLT$ 3", "*"):
            device = supply()
            device.process(text)
            self.assertIn("-102", next_error(device), text)

    def test_error_stops_the_rest_of_the_message(self):
        device = supply()
        device.process("VOLT 1;NOPE;VOLT 2")
        self.assertEqual(float(device.process("VOLT?")), 1.0)

    def test_responses_before_an_error_are_kept(self):
        device = supply()
        self.assertEqual(device.process("*OPC?;NOPE?;*TST?"), "1")

    def test_empty_message_does_nothing(self):
        device = supply()
        self.assertIsNone(device.process(""))
        self.assertIsNone(device.process(" ; "))
        self.assertEqual(next_error(device), '+0,"No error"')


class Parameters(unittest.TestCase):
    def test_exponents_and_suffixes(self):
        for text, want in (("1.5E1", 15.0), ("500MV", 0.5), ("500mV", 0.5), ("2V", 2.0), (".5", 0.5), ("+3.", 3.0)):
            device = supply()
            device.process(f"VOLT {text}")
            self.assertEqual(next_error(device), '+0,"No error"', text)
            self.assertAlmostEqual(float(device.process("VOLT?")), want, msg=text)

    def test_min_max_def(self):
        device = supply()
        device.process("VOLT MAX")
        self.assertEqual(float(device.process("VOLT?")), 30.0)
        device.process("VOLT MIN")
        self.assertEqual(float(device.process("VOLT?")), 0.0)
        device.process("CURR DEF")
        self.assertEqual(float(device.process("CURR?")), 1.0)

    def test_out_of_range_is_rejected_and_setting_kept(self):
        device = supply()
        device.process("VOLT 5")
        device.process("VOLT 31")
        self.assertIn("-222", next_error(device))
        self.assertEqual(float(device.process("VOLT?")), 5.0)

    def test_wrong_type(self):
        device = supply()
        device.process("VOLT abc")
        self.assertIn("-104", next_error(device))

    def test_wrong_suffix(self):
        device = supply()
        device.process("VOLT 5A")
        self.assertIn("-131", next_error(device))

    def test_missing_and_extra_parameters(self):
        device = supply()
        device.process("VOLT")
        self.assertIn("-109", next_error(device))
        device.process("VOLT 1,2")
        self.assertIn("-108", next_error(device))
        device.process("*RST 1")
        self.assertIn("-108", next_error(device))

    def test_empty_parameter(self):
        device = supply()
        device.process("VOLT 1,")
        self.assertIn("-104", next_error(device))

    def test_boolean_parameter(self):
        device = supply()
        device.process("OUTP 1")
        self.assertEqual(device.process("OUTP?"), "1")
        device.process("OUTP OFF")
        self.assertEqual(device.process("OUTP?"), "0")
        device.process("OUTP MAYBE")
        self.assertIn("-104", next_error(device))

    def test_mega_suffix_on_frequency(self):
        gen = SimGenerator(BenchModel(RegulatorParams()), noise=0.0)
        gen.process("FREQ 1MHZ")
        self.assertEqual(float(gen.process("FREQ?")), 1e6)
        gen.process("FREQ 5KHZ")
        self.assertEqual(float(gen.process("FREQ?")), 5e3)

    def test_choice_parameter_short_and_long(self):
        gen = SimGenerator(BenchModel(RegulatorParams()), noise=0.0)
        gen.process("FUNC SQUARE")
        self.assertEqual(gen.process("FUNC?"), "SQU")
        gen.process("FUNC TRI")
        self.assertEqual(gen.process("FUNC?"), "TRI")
        gen.process("FUNC NOISE")
        self.assertIn("-224", next_error(gen))

    def test_quoted_string_may_hold_separators(self):
        dmm = SimDmm(BenchModel(RegulatorParams()), noise=0.0)
        dmm.process('VOLT:DC:NPLC "a;b"')
        self.assertIn("-104", next_error(dmm))

    def test_unterminated_string(self):
        device = supply()
        device.process('VOLT "abc')
        self.assertIn("-150", next_error(device))

    def test_parse_number_without_limits_reports_nothing_odd(self):
        self.assertEqual(parse_number("10"), 10.0)
        with self.assertRaises(ScpiError) as caught:
            parse_number("10", low=20)
        self.assertEqual(caught.exception.code, -222)


class ErrorQueue(unittest.TestCase):
    def test_errors_come_back_first_in_first_out(self):
        device = supply()
        device.process("NOPE")
        device.process("VOLT 99")
        self.assertIn("-113", next_error(device))
        self.assertIn("-222", next_error(device))
        self.assertEqual(next_error(device), '+0,"No error"')

    def test_long_form_of_error_query(self):
        device = supply()
        device.process("NOPE")
        self.assertIn("-113", device.process("SYSTEM:ERROR:NEXT?"))

    def test_overflow_keeps_oldest_and_flags_the_loss(self):
        device = supply()
        for _ in range(ERROR_QUEUE_SIZE + 5):
            device.process("NOPE")
        for _ in range(ERROR_QUEUE_SIZE - 1):
            self.assertIn("-113", next_error(device))
        self.assertIn("-350", next_error(device))
        self.assertEqual(next_error(device), '+0,"No error"')

    def test_message_quotes_cannot_break_the_format(self):
        device = supply()
        device.process('VOLT "x"')
        line = next_error(device)
        self.assertEqual(line.count('"'), 2, line)


if __name__ == "__main__":
    unittest.main()
