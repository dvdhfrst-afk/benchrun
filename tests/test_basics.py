"""Units, limits and the expression language."""

import math
import unittest

from benchrun.errors import ExpressionError
from benchrun.expr import Expression
from benchrun.limits import ERROR, FAIL, INFO, PASS, Limit
from benchrun.units import compatible, convert, split_unit


class Units(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_unit("mV"), (1e-3, "V"))
        self.assertEqual(split_unit("V"), (1.0, "V"))
        self.assertEqual(split_unit("kHz"), (1e3, "Hz"))
        self.assertEqual(split_unit("mVpp"), (1e-3, "Vpp"))
        self.assertEqual(split_unit("uA"), (1e-6, "A"))
        self.assertEqual(split_unit("\u00b5A"), (1e-6, "A"))

    def test_decibels_are_not_deci_anything(self):
        self.assertEqual(split_unit("dB"), (1.0, "dB"))

    def test_seconds_and_percent(self):
        self.assertEqual(split_unit("ms"), (1e-3, "s"))
        self.assertEqual(split_unit("%"), (1.0, "%"))

    def test_free_form_unit_is_its_own_base(self):
        self.assertEqual(split_unit("mV/V"), (1.0, "mV/V"))

    def test_convert(self):
        self.assertAlmostEqual(convert(1.5, "V", "mV"), 1500.0)
        self.assertAlmostEqual(convert(250.0, "mA", "A"), 0.25)
        self.assertEqual(convert(2.0, "Hz", "Hz"), 2.0)

    def test_convert_refuses_incompatible(self):
        for a, b in (("V", "A"), ("Vpp", "V"), ("Vrms", "mVpp")):
            with self.assertRaises(ValueError, msg=(a, b)):
                convert(1.0, a, b)

    def test_compatible(self):
        self.assertTrue(compatible("V", "mV"))
        self.assertTrue(compatible("mVrms", "Vrms"))
        self.assertFalse(compatible("V", "Vrms"))
        self.assertFalse(compatible("", "V"))


class Limits(unittest.TestCase):
    def test_inside_both_limits(self):
        self.assertEqual(Limit(1, 2).evaluate(1.5), PASS)

    def test_limits_are_inclusive(self):
        self.assertEqual(Limit(1, 2).evaluate(1.0), PASS)
        self.assertEqual(Limit(1, 2).evaluate(2.0), PASS)

    def test_just_outside(self):
        self.assertEqual(Limit(1, 2).evaluate(math.nextafter(1.0, 0.0) if hasattr(math, "nextafter") else 0.9999999), FAIL)
        self.assertEqual(Limit(1, 2).evaluate(2.0000001), FAIL)

    def test_one_sided(self):
        self.assertEqual(Limit(low=1).evaluate(1e30), PASS)
        self.assertEqual(Limit(low=1).evaluate(0.5), FAIL)
        self.assertEqual(Limit(high=1).evaluate(-1e30), PASS)
        self.assertEqual(Limit(high=1).evaluate(1.5), FAIL)

    def test_zero_limits_are_limits(self):
        self.assertEqual(Limit(high=0).evaluate(0.0), PASS)
        self.assertEqual(Limit(high=0).evaluate(1.0), FAIL)
        self.assertTrue(Limit(low=0).is_limited)

    def test_no_limits_is_information(self):
        self.assertEqual(Limit().evaluate(123.0), INFO)
        self.assertFalse(Limit().is_limited)

    def test_a_bad_value_never_passes(self):
        for bad in (None, float("nan"), float("inf"), float("-inf")):
            self.assertEqual(Limit(0, 1).evaluate(bad), FAIL, bad)
            self.assertEqual(Limit(low=0).evaluate(bad), FAIL, bad)
            self.assertEqual(Limit().evaluate(bad), ERROR, bad)

    def test_describe(self):
        self.assertEqual(Limit(1, 2).describe("V"), "1 to 2 V")
        self.assertEqual(Limit(low=1).describe("V"), ">= 1 V")
        self.assertEqual(Limit(high=2).describe(), "<= 2")
        self.assertEqual(Limit().describe("V"), "none")


class Expressions(unittest.TestCase):
    def value(self, text, **env):
        return Expression(text).evaluate(env)

    def test_arithmetic_and_precedence(self):
        self.assertEqual(self.value("1 + 2 * 3"), 7.0)
        self.assertEqual(self.value("(1 + 2) * 3"), 9.0)
        self.assertEqual(self.value("2 ** 3"), 8.0)
        self.assertEqual(self.value("-2 + +3"), 1.0)
        self.assertEqual(self.value("1 / 4"), 0.25)

    def test_names(self):
        self.assertEqual(self.value("vin * iin", vin=12.0, iin=0.5), 6.0)
        self.assertEqual(Expression("a + max(b, c)").names, {"a", "b", "c"})

    def test_list_functions(self):
        data = [3.0, 1.0, 2.0]
        self.assertEqual(self.value("min(x)", x=data), 1.0)
        self.assertEqual(self.value("max(x)", x=data), 3.0)
        self.assertEqual(self.value("first(x)", x=data), 3.0)
        self.assertEqual(self.value("last(x)", x=data), 2.0)
        self.assertEqual(self.value("mean(x)", x=data), 2.0)
        self.assertEqual(self.value("span(x)", x=data), 2.0)
        self.assertEqual(self.value("sum(x)", x=data), 6.0)
        self.assertEqual(self.value("count(x)", x=data), 3.0)

    def test_math_functions(self):
        self.assertEqual(self.value("sqrt(16)"), 4.0)
        self.assertEqual(self.value("log10(1000)"), 3.0)
        self.assertEqual(self.value("abs(-2)"), 2.0)
        self.assertEqual(self.value("max(1, 2, 3)"), 3.0)

    def test_undefined_math_is_an_expression_error(self):
        for text in ("1 / 0", "log10(0)", "sqrt(-1)", "(-8) ** 0.5 * 1"):
            with self.assertRaises(ExpressionError, msg=text):
                self.value(text)

    def test_overflow_is_an_expression_error(self):
        with self.assertRaises(ExpressionError):
            self.value("10.0 ** 400")

    def test_unknown_name_at_evaluation(self):
        with self.assertRaisesRegex(ExpressionError, "unknown name"):
            self.value("nope")

    def test_list_result_is_refused(self):
        with self.assertRaisesRegex(ExpressionError, "list"):
            self.value("x", x=[1.0])

    def test_missing_value_in_a_list_is_an_expression_error(self):
        with self.assertRaises(ExpressionError):
            self.value("max(x)", x=[1.0, None])
        with self.assertRaises(ExpressionError):
            self.value("mean(x)", x=[1.0, None])

    def test_single_scalar_to_list_function(self):
        with self.assertRaises(ExpressionError):
            self.value("first(x)", x=1.0)

    def test_empty_list(self):
        with self.assertRaises(ExpressionError):
            self.value("mean(x)", x=[])

    def test_nothing_dangerous_gets_through(self):
        for text in (
            "__import__('os')",
            "open('x')",
            "x.y",
            "x[0]",
            "[1, 2]",
            "(1, 2)",
            "'text'",
            "lambda: 1",
            "1 if 2 else 3",
            "1 < 2",
            "True",
            "None",
            "max(key=1)",
            "print(1)",
            "x; y",
            "1 % 2",
            "1 // 2",
            "",
        ):
            with self.assertRaises(ExpressionError, msg=text):
                Expression(text)

    def test_shape_check(self):
        lists = {"vout", "vin"}
        for good in ("span(vout)", "max(vout) - min(vout)", "first(vout) / last(vin)", "max(a, b)", "abs(max(vout))"):
            self.assertIsNone(Expression(good).shape_problem(lists), good)
        for bad in ("vout * 2", "vout", "abs(vout)", "max(vout, 1)", "first(a)", "mean(1)", "max(1)", "sqrt(vout)"):
            self.assertIsNotNone(Expression(bad).shape_problem(lists), bad)


if __name__ == "__main__":
    unittest.main()
