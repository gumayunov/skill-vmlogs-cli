import json
import unittest

from vmlogs_cli.output import (
    DEFAULT_HIDE,
    field_lines,
    is_hidden,
    json_line,
    short_line,
    sort_records,
)

ROW = {
    "_time": "2026-10-26T07:03:12.345Z",
    "_stream_id": "0000abc",
    "_stream": '{kubernetes.container_name="api"}',
    "_msg": "connection refused\nretrying",
    "kubernetes.container_name": "api",
    "kubernetes.pod_name": "shop-api-7f9c",
    "kubernetes.pod_labels.app": "shop",
    "kubernetes.pod_ip": "10.0.0.7",
    "level": "error",
    "trace_id": "ab12",
}
FULL = (
    "2026-10-26T07:03:12.345Z api ERROR connection refused⏎retrying"
    "  kubernetes.pod_name=shop-api-7f9c trace_id=ab12"
)


class ShortLineTest(unittest.TestCase):
    def test_record(self):
        self.assertEqual(short_line(ROW, 1000, DEFAULT_HIDE), FULL)

    def test_other_collector_schema(self):
        row = {
            "_time": "t",
            "_msg": "ok",
            "k8s.container_name": "worker",
            "json.level": "INFO",
            "k8s.pod_labels.app": "x",
        }
        self.assertEqual(short_line(row, 1000, DEFAULT_HIDE), "t worker INFO ok")

    def test_no_container_no_level(self):
        self.assertEqual(short_line({"_time": "t", "_msg": "hello"}, 1000, DEFAULT_HIDE), "t hello")

    def test_context_whitelist(self):
        line = short_line(ROW, 1000, DEFAULT_HIDE, ["trace_id", "missing"])
        self.assertEqual(
            line, "2026-10-26T07:03:12.345Z api ERROR connection refused⏎retrying  trace_id=ab12"
        )

    def test_lone_cr_is_replaced(self):
        row = {"_time": "t", "_msg": "a\rb\r\nc\nd"}
        self.assertEqual(short_line(row, 1000, DEFAULT_HIDE), "t a⏎b⏎c⏎d")

    def test_empty_msg_has_no_trailing_space(self):
        self.assertEqual(short_line({"_time": "t", "_msg": ""}, 1000, DEFAULT_HIDE), "t")

    def test_non_string_values(self):
        row = {"_time": "t", "_msg": "m", "code": 500, "extra": {"a": "é"}}
        self.assertEqual(short_line(row, 1000, DEFAULT_HIDE), 't m  code=500 extra={"a": "é"}')

    def test_width_boundary(self):
        row = {"_time": "t", "_msg": "xxxx"}  # «t xxxx» — ровно 6 символов
        self.assertEqual(short_line(row, 6, DEFAULT_HIDE), "t xxxx")
        self.assertTrue(short_line(row, 5, DEFAULT_HIDE).startswith("t xxx… [truncated: 5/6"))

    def test_context_field_beats_hide_list(self):
        row = {"_time": "t", "_msg": "m", "_stream": "s", "file": "f"}
        self.assertEqual(
            short_line(row, 1000, DEFAULT_HIDE, ["_stream", "file"]), "t m  _stream=s file=f"
        )

    def test_truncation_marker(self):
        self.assertEqual(
            short_line(ROW, 30, DEFAULT_HIDE),
            f"{FULL[:30]}… [truncated: 30/{len(FULL)} chars, get 0000abc/2026-10-26T07:03:12.345Z]",
        )

    def test_width_zero_keeps_everything(self):
        self.assertEqual(short_line(ROW, 0, DEFAULT_HIDE), FULL)

    def test_truncation_without_stream_id(self):
        row = {"_time": "t", "_msg": "x" * 20}
        self.assertEqual(short_line(row, 5, DEFAULT_HIDE), "t xxx… [truncated: 5/22 chars]")

    def test_stats_row(self):
        row = {"kubernetes.container_name": "api", "c": "12"}
        self.assertEqual(short_line(row, 1000, DEFAULT_HIDE), "kubernetes.container_name=api c=12")

    def test_stats_row_is_truncated(self):
        row = {"_stream": "x" * 50, "c": "1"}
        self.assertEqual(short_line(row, 10, DEFAULT_HIDE), "_stream=xx… [truncated: 10/62 chars]")

    def test_stats_row_with_time_bucket(self):
        row = {"_time": "2026-10-26T07:00:00Z", "c": "3"}
        self.assertEqual(short_line(row, 1000, DEFAULT_HIDE), "_time=2026-10-26T07:00:00Z c=3")


class SortRecordsTest(unittest.TestCase):
    def test_ascending_with_mixed_fractions(self):
        rows = [
            {"_time": "2026-10-26T11:58:54.4847Z", "_msg": "c"},
            {"_time": "2026-10-26T11:58:54Z", "_msg": "b"},
            {"_time": "2026-10-26T11:58:53.9Z", "_msg": "a"},
        ]
        self.assertEqual([r["_msg"] for r in sort_records(rows)], ["a", "b", "c"])

    def test_missing_or_non_string_time_goes_first(self):
        rows = [
            {"_time": "2026-10-26T11:58:54Z", "_msg": "b"},
            {"_time": None, "_msg": "n"},
            {"_time": 5, "_msg": "i"},
            {"_msg": "m"},
        ]
        self.assertEqual([r["_msg"] for r in sort_records(rows)][-1], "b")

    def test_stats_rows_keep_order(self):
        rows = [{"c": "2"}, {"c": "1"}]
        self.assertEqual(sort_records(rows), rows)


class JsonLineTest(unittest.TestCase):
    def test_hides_and_truncates(self):
        out = json.loads(json_line(ROW, 10, DEFAULT_HIDE))
        self.assertEqual(out["_msg"], "connection…(+17)")
        self.assertEqual(out["trace_id"], "ab12")
        for hidden in ("_stream_id", "_stream", "kubernetes.pod_ip", "kubernetes.pod_labels.app"):
            self.assertNotIn(hidden, out)

    def test_long_non_string_is_cut(self):
        row = {"_msg": "m", "obj": {"a": "x\x00" * 20}}
        dumped = json.dumps(row["obj"], ensure_ascii=False).replace("\x00", "")
        out = json.loads(json_line(row, 10, []))
        self.assertTrue(out["obj"].startswith(dumped[:10] + "…(+"))
        self.assertNotIn("\x00", out["obj"])
        self.assertEqual(out["obj"], f"{dumped[:10]}…(+{len(dumped) - 10})")

    def test_short_non_string_is_kept(self):
        out = json.loads(json_line({"_msg": "m", "n": 5, "o": {"a": 1}}, 100, []))
        self.assertEqual((out["n"], out["o"]), (5, {"a": 1}))

    def test_width_zero(self):
        self.assertEqual(json.loads(json_line(ROW, 0, []))["_msg"], ROW["_msg"])


class HiddenTest(unittest.TestCase):
    def test_prefix_and_exact(self):
        self.assertTrue(is_hidden("file", DEFAULT_HIDE))
        self.assertFalse(is_hidden("filename", DEFAULT_HIDE))
        self.assertTrue(is_hidden("vector.host", DEFAULT_HIDE))
        self.assertFalse(is_hidden("kubernetes.pod_name", DEFAULT_HIDE))

    def test_field_lines(self):
        values = [
            {"value": "_msg", "hits": 5},
            {"value": "_stream", "hits": 9},
            {"value": "level", "hits": 5},
            {"value": "trace_id", "hits": 2},
        ]
        self.assertEqual(
            field_lines(values, DEFAULT_HIDE), (["5\t_msg", "5\tlevel", "2\ttrace_id"], 1)
        )


if __name__ == "__main__":
    unittest.main()
