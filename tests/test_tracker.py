import unittest
from datetime import date

from tracker import (
    Candidate,
    Result,
    SourceError,
    evaluate_booster,
    format_discord,
    kind_of_product,
    parse_date_from_text,
    price_for_uuid,
    select_candidates,
    model_net_ev,
    net_price,
    confidence_score,
    risk_label,
    movement_arrow,
    price_anomaly,
    audit_booster_config,
)


def paper(normal=None, foil=None):
    p = {}
    if normal is not None:
        p["normal"] = {"2026-10-24": normal}
    if foil is not None:
        p["foil"] = {"2026-10-24": foil}
    return {"paper": {"tcgplayer": {"retail": p}}}


class SourceTests(unittest.TestCase):
    def test_finishes_never_substitute(self):
        prices = {"one": paper(normal=2.5, foil=6), "two": paper(normal=3.2)}
        self.assertEqual(price_for_uuid(prices, "one", False)[0], 2.5)
        self.assertEqual(price_for_uuid(prices, "one", True)[0], 6)
        self.assertIsNone(price_for_uuid(prices, "two", True)[0])
        self.assertIsNone(price_for_uuid(prices, "missing", False)[0])

    def test_select_single_exact_products(self):
        for name in ("Aetherdrift Play Booster Box", "Aetherdrift Sleeved Play Booster", "Collector Booster Display", "Play Booster Bundle"):
            self.assertIsNone(kind_of_product(name))
        self.assertEqual(kind_of_product("Aetherdrift - Play Booster Pack"), "play")
        self.assertEqual(kind_of_product("Aetherdrift - Collector Booster Pack"), "collector")
        items = select_candidates([{"code": "DFT", "name": "Aetherdrift", "releaseDate": "2025-02-14", "tcgplayerGroupId": 1,
                                    "sealedProduct": [{"name": "Aetherdrift - Play Booster Pack", "identifiers": {"tcgplayerProductId": "123"},
                                                       "contents": {"pack": [{"code": "play", "set": "DFT"}]}},
                                                      {"name": "Aetherdrift - Play Booster Box", "identifiers": {"tcgplayerProductId": "125"}}]}], date(2026, 10, 24), 2024)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].product_id, 123)
        self.assertEqual(items[0].booster_code, "play")

    def test_weighted_ev_and_exact_foil(self):
        config = {
            "boosters": [{"weight": 1, "contents": {"common": 4, "foil": 1}}],
            "sheets": {
                "common": {"foil": False, "cards": {"a": 1, "b": 1, "c": 1, "d": 1}},
                "foil": {"foil": True, "cards": {"f": 1}},
            },
        }
        prices = {"a": paper(1), "b": paper(2), "c": paper(3), "d": paper(4), "f": paper(0.25, 10)}
        ev, median, coverage, day = evaluate_booster(config, prices, samples=150, seed=1)
        self.assertAlmostEqual(ev, 20.0)
        self.assertEqual(median, 20.0)
        self.assertEqual(coverage, 1.0)
        self.assertEqual(day, "2026-10-24")

    def test_unpriced_finishes_are_rejected(self):
        config = {"boosters": [{"weight": 1, "contents": {"regular": 4, "special": 1}}],
                  "sheets": {"regular": {"foil": False, "cards": {"a": 1, "b": 1, "c": 1, "d": 1}},
                             "special": {"foil": True, "cards": {"f": 1}}}}
        prices = {x: paper(normal=1) for x in ("a", "b", "c", "d", "f")}
        with self.assertRaises(SourceError):
            evaluate_booster(config, prices, samples=10)

    def test_discord_is_monospaced_and_under_limit(self):
        results = [Result("Test Set " + str(i), str(i), "play", 4 + i, 3 + i, 2 + i, 80, 1, "2026-10-24", i)
                   for i in range(8)]
        text = format_discord("play", results, date(2026, 10, 24), date(2026, 10, 24), 8)
        self.assertEqual(text.count("```text"), 0)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("Pack $4.00 | EV $3.00 | Median $2.00 | ROI 80%", text)


    def test_new_metrics(self):
        self.assertEqual(net_price(0.50), 0.0)
        self.assertAlmostEqual(net_price(10), 8.7)
        self.assertEqual(risk_label(10, 2), "High")
        self.assertEqual(risk_label(10, 9), "Low")
        self.assertEqual(movement_arrow(2, 5), "▲3")
        self.assertEqual(movement_arrow(5, 2), "▼3")
        self.assertEqual(movement_arrow(2, None), "NEW")
        self.assertTrue(price_anomaly(1, 5))
        self.assertFalse(price_anomaly(4, 5))
        self.assertLess(confidence_score(.97, "2026-10-20", date(2026, 10, 24),
                                         "profile limited"),
                        confidence_score(1, "2026-10-24", date(2026, 10, 24),
                                         "All 5 | Dragons"))
        config = {"boosters": [{"weight": 1, "contents": {"common": 4}}],
                  "sheets": {"common": {"cards": {"a": 1, "b": 1, "c": 1, "d": 1}}}}
        prices = {uid: paper(normal=2) for uid in "abcd"}
        self.assertAlmostEqual(model_net_ev(config, prices), 6.96)


    def test_weighted_without_replacement_ev_is_not_naive(self):
        config = {"boosters": [{"weight": 1, "contents": {"cards": 4}}],
                  "sheets": {"cards": {"cards": {"a": 100, "b": 1, "c": 1, "d": 1}}}}
        prices = {"a": paper(normal=1), "b": paper(normal=100),
                  "c": paper(normal=100), "d": paper(normal=100)}
        ev, median, coverage, _ = evaluate_booster(config, prices, samples=200, seed=7)
        self.assertAlmostEqual(ev, 301)
        self.assertAlmostEqual(median, 301)
        self.assertEqual(coverage, 1)
        self.assertTrue(any("Monte Carlo" in x for x in audit_booster_config(config)))

    def test_audit_flags_color_balance_and_fixed_sheet(self):
        config = {"boosters": [{"weight": 1, "contents": {"fixed": 1}}],
                  "sheets": {"fixed": {"fixed": True, "balanceColors": True,
                                       "cards": {"a": 1}}}}
        findings = audit_booster_config(config)
        self.assertTrue(any("fixed-card" in x for x in findings))
        self.assertTrue(any("color-balanced" in x for x in findings))

    def test_date_parse(self):
        self.assertEqual(parse_date_from_text("2026-10-24T03:20:00Z"), date(2026, 10, 24))



    def test_profiles_and_top15(self):
        from tracker import booster_card_profile
        values = [Result(f"Set {i}", str(i), "play", 5, 5, 3, 100 - i, 1,
                         "2026-10-24", i, "WUBRG | Dragons, Artifacts") for i in range(19)]
        message = format_discord("play", values, date(2026, 10, 24), date(2026, 10, 24), 15)
        self.assertIn("TOP 15", message)
        self.assertEqual(message.count("WUBRG — Dragons"), 15)
        self.assertNotIn("Before fees", message)
        self.assertNotIn("Sources:", message)
        self.assertLessEqual(len(message), 2000)
        records = [{"uuid": str(i), "name": f"Card {i}", "colors": [["W"], ["U"], ["B"], ["R"], ["G"]][i % 5],
                    "types": ["Creature"], "subtypes": ["Dragon"]} for i in range(15)]
        config = {"boosters": [{"weight": 1, "contents": {"creatures": 2}}],
                  "sheets": {"creatures": {"cards": {str(i): 1 for i in range(15)}}}}
        profile = booster_card_profile(config, {"cards": records})
        self.assertIn("All 5", profile)
        self.assertIn("Dragon", profile)


if __name__ == "__main__":
    unittest.main()
