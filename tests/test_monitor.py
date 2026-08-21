import tempfile
import unittest
from pathlib import Path

import monitor


FIXTURE = Path(__file__).parent / "fixtures" / "repertoire.html"


class RepertoireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = monitor.parse_repertoire(FIXTURE.read_text(encoding="utf-8"))

    def test_parses_all_cards(self):
        self.assertEqual(3, len(self.events))
        self.assertEqual("3. Sep", self.events[0].date)
        self.assertEqual("18:00", self.events[0].time)
        self.assertTrue(self.events[0].sold_out)

    def test_filters_available_riga_events(self):
        available = monitor.available_in_riga(self.events)
        self.assertEqual(["TESTA IZRĀDE"], [event.title for event in available])
        self.assertEqual("https://www.bilesuparadize.lv/lv/event/123456", available[0].url)

    def test_state_round_trip(self):
        available = monitor.available_in_riga(self.events)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            monitor.save_state(path, available)
            self.assertEqual({available[0].event_id}, monitor.load_state(path))

    def test_message_escapes_html(self):
        event = monitor.Performance("A & B", "1. Sep", "19:00", "Rīga <zāle>", "https://example.com/?a=1&b=2", False)
        subject, text, body = monitor.make_message([event])
        self.assertIn("A & B", text)
        self.assertIn("A &amp; B", body)
        self.assertIn("Rīga &lt;zāle&gt;", body)
        self.assertIn("&amp;", body)
        self.assertIn("появились билеты", subject)


if __name__ == "__main__":
    unittest.main()
