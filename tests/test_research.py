import csv
import io
import unittest

from quantify.research import demo_csv, evaluate, parse_panel, signals


class FactorTests(unittest.TestCase):
    def test_future_data_does_not_change_past_signal(self):
        _, panel = parse_panel(demo_csv())
        before = signals(panel, 20)
        panel[21]["DEMO1"]["total_return_index"] *= 5
        panel[21]["DEMO1"]["quality"] = 100
        self.assertEqual(before, signals(panel, 20))

    def test_future_data_does_not_change_first_fold_choice(self):
        source=demo_csv()
        rows=list(csv.DictReader(io.StringIO(source)))
        before=evaluate(source, source="test")
        cutoff=before["folds"][0]["test_signal_start"]
        for row in rows:
            if row["date"] >= cutoff:
                row["total_return_index"] = str(float(row["total_return_index"])*1.5)
        buf=io.StringIO()
        writer=csv.DictWriter(buf,fieldnames=rows[0].keys())
        writer.writeheader();writer.writerows(rows)
        after=evaluate(buf.getvalue(),source="test")
        self.assertEqual(before["folds"][0]["training_rates"],after["folds"][0]["training_rates"])
        self.assertEqual(before["folds"][0]["chosen_factor"],after["folds"][0]["chosen_factor"])

    def test_oos_labels_mature_before_test_and_costs_apply(self):
        for horizon in (3,6,12):
            with self.subTest(horizon=horizon):
                result=evaluate(horizon=horizon,cost_bps=0)
                self.assertTrue(result["is_demo"])
                self.assertGreater(result["selected"]["observations"],0)
                for fold in result["folds"]:
                    self.assertLess(fold["train_last_label"],fold["test_signal_start"])
                charged=evaluate(horizon=horizon,cost_bps=100)
                # Baseline selection never changes with transaction cost.
                self.assertAlmostEqual(result["baseline"]["mean_return"]-charged["baseline"]["mean_return"],.01)

    def test_rejects_future_factor_availability(self):
        source=demo_csv().replace(",2017-01-01",",2017-02-01",1)
        with self.assertRaisesRegex(ValueError,"未来数据"):
            parse_panel(source)

    def test_rejects_missing_duplicate_or_invalid_rows(self):
        source=demo_csv()
        lines=source.splitlines()
        for invalid in ('\n'.join(lines[:1]+lines[2:]),source+'\n'+lines[1],source.replace('100', 'nan',1),source[:100]):
            with self.subTest(invalid=invalid[:30]),self.assertRaises(ValueError):
                parse_panel(invalid)

    def test_invalid_parameters(self):
        for kwargs in ({"horizon":4},{"top_k":5},{"cost_bps":-1},{"source":""}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                evaluate(**kwargs)


if __name__ == "__main__":
    unittest.main()
