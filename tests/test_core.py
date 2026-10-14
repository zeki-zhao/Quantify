import math
import unittest
from statistics import NormalDist

from quantify.core import fundamentals, quantile, simulate, valuation
from quantify.data import DemoDataProvider
from quantify.service import analyze, export_csv


class ValuationTests(unittest.TestCase):
    def scenarios(self, amount=10):
        return [{"amount": amount, "multiple": 20, "note": "利润恢复假设"}] * 3

    def test_units_and_returns(self):
        result = valuation(30, 5, self.scenarios())["results"][0]
        self.assertEqual(result["target_price"], 40)
        self.assertAlmostEqual(result["price_return"], 1/3)

    def test_insurance_ev(self):
        result = valuation(50, 100, [{"amount": 10000, "multiple": .7, "note": "EV假设"}]*3, "pev")
        self.assertEqual(result["results"][0]["target_price"], 70)

    def test_nonpositive_profit_not_negative_price(self):
        for amount in (0, -10):
            self.assertIsNone(valuation(30, 5, self.scenarios(amount))["results"][0]["target_price"])

    def test_invalid_valuation(self):
        for price in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                valuation(price, 5, self.scenarios())
        with self.assertRaises(ValueError):
            valuation(30, 5, [{"amount": 10, "multiple": 20, "note": ""}]*3)

    def test_industrial_and_insurance_metrics(self):
        provider = DemoDataProvider()
        industrial = fundamentals(provider.get_stock("301358.SZ"))
        self.assertAlmostEqual(industrial["经营现金流/归母净利润"], .9)
        self.assertAlmostEqual(industrial["简化自由现金流（亿元）"], 1)
        insurance = fundamentals(provider.get_stock("601318.SH"))
        self.assertIn("P/EV", insurance)
        self.assertNotIn("经营现金流/归母净利润", insurance)

    def test_no_scenario_to_probability_conversion(self):
        payload = {"symbol":"301358.SZ", "scenarios":self.scenarios(), "simulation":{"paths":100}}
        first = analyze(payload)
        payload["scenarios"] = self.scenarios(100)
        second = analyze(payload)
        self.assertEqual(first["simulation"], second["simulation"])

    def test_export_metadata_and_formula_injection(self):
        result = export_csv({"source":"合成数据", "note":"=1+1", "rate":-.1, "model":{"seed":42}})
        self.assertIn("'=1+1", result)
        self.assertIn("rate,-0.1", result)
        self.assertIn("model.seed,42", result)


class SimulationTests(unittest.TestCase):
    def test_quantile_interpolation(self):
        self.assertEqual(quantile([0,10], .25), 2.5)

    def test_deterministic_paths_and_dividends(self):
        result = simulate(mu=.1, sigma=0, dividend_yield=.03, paths=100)
        for month, data in result["horizons"].items():
            t = int(month)/12
            self.assertAlmostEqual(data["price"]["mean"], math.expm1(.1*t))
            self.assertAlmostEqual(data["total"]["mean"], math.expm1(.13*t))
            self.assertEqual(data["price"]["p_up"], 1)
            self.assertEqual(data["price"]["p_touch"]["10"], 0)

    def test_flat_and_no_dividend(self):
        result = simulate(mu=0,sigma=0,dividend_yield=0,paths=100)
        for data in result["horizons"].values():
            self.assertEqual(data["price"], data["total"])
            self.assertEqual(data["price"]["p_flat"], 1)

    def test_falling_path_barriers(self):
        result = simulate(mu=math.log(.6),sigma=0,dividend_yield=0,paths=100)
        self.assertEqual(result["horizons"]["12"]["price"]["p_touch"]["40"], 1)
        self.assertEqual(result["horizons"]["3"]["price"]["p_touch"]["20"], 0)

    def test_seed_and_probability_invariants(self):
        a = simulate(paths=200)
        self.assertEqual(a, simulate(paths=200))
        previous = dict.fromkeys(("10","20","30","40"),0)
        for data in a["horizons"].values():
            price,total=data["price"],data["total"]
            self.assertAlmostEqual(price["p_up"]+price["p_down"]+price["p_flat"],1)
            self.assertEqual(sum(price["histogram"]["counts"]),200)
            probs = list(price["p_touch"].values())
            self.assertEqual(probs,sorted(probs,reverse=True))
            for barrier,p in price["p_touch"].items():
                self.assertGreaterEqual(p,previous[barrier])
                self.assertLessEqual(total["p_touch"][barrier],p)
                previous[barrier]=p

    def test_terminal_distribution_against_analytic_gbm(self):
        mu,sigma=.06,.3
        data=simulate(mu=mu,sigma=sigma,dividend_yield=0,paths=6000)["horizons"]["12"]["price"]
        expected_up=NormalDist().cdf((mu-.5*sigma*sigma)/sigma)
        self.assertAlmostEqual(data["p_up"],expected_up,delta=.025)
        self.assertAlmostEqual(data["mean"],math.expm1(mu),delta=.02)

    def test_parameter_validation(self):
        for kwargs in ({"sigma":-1},{"mu":float("nan")},{"paths":0},{"paths":10001},{"paths":100.5},{"seed":True},{"dividend_yield":-.1}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                simulate(**kwargs)


if __name__ == "__main__":
    unittest.main()
