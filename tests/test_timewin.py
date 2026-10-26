import unittest
from datetime import datetime, timezone

from vmlogs_cli.errors import CliError
from vmlogs_cli.timewin import parse_point, window

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


class ParsePointTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_point("90s", NOW), "2026-10-26T11:58:30Z")
        self.assertEqual(parse_point("15m", NOW), "2026-10-26T11:45:00Z")
        self.assertEqual(parse_point("2h", NOW), "2026-10-26T10:00:00Z")
        self.assertEqual(parse_point("1d", NOW), "2026-10-25T12:00:00Z")
        self.assertEqual(parse_point("1w", NOW), "2026-10-19T12:00:00Z")

    def test_huge_duration(self):
        with self.assertRaisesRegex(CliError, "слишком большая"):
            parse_point("99999999999w", NOW)

    def test_rfc3339(self):
        self.assertEqual(parse_point("2026-10-26T07:03:12Z", NOW), "2026-10-26T07:03:12Z")
        self.assertEqual(parse_point("2026-10-26T10:03:12+03:00", NOW), "2026-10-26T07:03:12Z")
        self.assertEqual(
            parse_point("2026-10-26T07:03:12.345Z", NOW), "2026-10-26T07:03:12.345000Z"
        )
        self.assertEqual(
            parse_point("2026-10-26T07:03:12.123456789Z", NOW), "2026-10-26T07:03:12.123456Z"
        )

    def test_errors(self):
        for bad in ("yesterday", "10", "2026-10-26T07:03:12"):
            with self.subTest(bad=bad), self.assertRaises(CliError):
                parse_point(bad, NOW)

    def test_z_only_at_the_end(self):
        # Раньше заменялись все «Z», и «2026-10-26Z07…» превращалось в валидную дату.
        with self.assertRaises(CliError):
            parse_point("2026-10-26Z07:03:12Z", NOW)

    def test_lowercase_z(self):
        self.assertEqual(parse_point("2026-10-26T07:03:12z", NOW), "2026-10-26T07:03:12Z")


class WindowTest(unittest.TestCase):
    def test_since(self):
        self.assertEqual(window("1h", None, None, NOW), ("2026-10-26T11:00:00Z", None))

    def test_start_overrides_since(self):
        self.assertEqual(
            window("1h", "2026-10-26T07:00:00Z", "30m", NOW),
            ("2026-10-26T07:00:00Z", "2026-10-26T11:30:00Z"),
        )

    def test_start_after_end(self):
        with self.assertRaisesRegex(
            CliError, "начало окна позже конца: 2026-10-26T11:00:00Z > 2026-10-26T10:00:00Z"
        ):
            window("1h", "2026-10-26T11:00:00Z", "2026-10-26T10:00:00Z", NOW)

    def test_since_after_end(self):
        with self.assertRaisesRegex(CliError, "начало окна позже конца"):
            window("1h", None, "2h", NOW)


if __name__ == "__main__":
    unittest.main()
