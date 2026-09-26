import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class TransitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_route_version_review_publish_and_snapshot_isolation(self):
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"])["minutes"], 23)
        disruption = self.db.create_disruption("planner-01", {"code": "D-001", "name": "会展站跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")
        v1 = disruption["draft_version_id"]
        self.db.add_change(v1, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        with self.assertRaises(DomainError):
            self.db.transition(v1, "planner-01", "planner", "publish")
        self.db.transition(v1, "planner-01", "planner", "submit")
        self.db.transition(v1, "reviewer-01", "reviewer", "approve")
        published = self.db.transition(v1, "reviewer-01", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)

        v2 = self.db.create_version_copy(disruption["id"], v1, "planner-02", "planner")["id"]
        self.db.add_change(v2, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)
        # Publishing v2 as a draft snapshot does not alter the old published v1.
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v1)["minutes"], 31)
        self.assertEqual(self.db.route(self.stops["S1"], self.stops["S5"], v2)["minutes"], 18)

    def test_cross_midnight_times_and_bad_data_isolation(self):
        times = self.db.trip_times(self.db.list_trips()[0]["id"])
        self.assertEqual(times[0]["clock"], "23:50")
        self.assertEqual(times[-1]["service_minute"], 1461)
        self.assertEqual(times[-1]["clock"], "00:21")
        self.assertEqual(times[-1]["day_offset"], 1)

        fresh = Database(Path(self.tmp.name) / "bad.db")
        result = fresh.import_base("planner-01", {
            "lines": [{"code": "B1", "name": "错误线路"}],
            "stops": [{"code": "B-S1", "name": "站点一", "latitude": 31, "longitude": 121}],
            "line_stops": [
                {"line_code": "B1", "stop_code": "B-S1", "sequence": 0, "travel_minutes_from_previous": 0},
                {"line_code": "B1", "stop_code": "NO-SUCH", "sequence": 1, "travel_minutes_from_previous": 5},
            ],
            "trips": [],
        }, "planner")
        self.assertFalse(result["accepted"])
        self.assertTrue(fresh.list_import_errors())
        self.assertEqual(fresh.list_lines(), [])

    def test_accessibility_and_conflict_validation(self):
        disruption = self.db.create_disruption("planner-01", {"code": "D-002", "name": "站点无障碍设施故障", "starts_at": "2026-09-24T00:00:00+08:00", "ends_at": "2026-09-25T00:00:00+08:00"}, "planner")
        version = disruption["draft_version_id"]
        self.db.add_change(version, "planner-01", {"kind": "accessibility_change", "stop_id": self.stops["S4"], "accessible": False}, "planner")
        normal = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=False)
        accessible = self.db.route(self.stops["S1"], self.stops["S5"], version, require_accessible=True)
        self.assertEqual(normal["minutes"], 23)
        self.assertEqual(accessible["minutes"], 31)
        with self.assertRaises(DomainError):
            self.db.add_change(version, "viewer", {"kind": "stop_closure", "stop_id": self.stops["S2"]}, "viewer")


class ConflictCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed_demo(self.db)
        self.stops = {row["code"]: row["id"] for row in self.db.list_stops()}

    def tearDown(self):
        self.tmp.cleanup()

    def _disruption(self, code, starts, ends, actor="planner-01"):
        return self.db.create_disruption(actor, {"code": code, "name": code + " 方案", "starts_at": starts, "ends_at": ends}, "planner")

    def _publish(self, version_id, actor="planner-01", reviewer="reviewer-01"):
        self.db.transition(version_id, actor, "planner", "submit")
        self.db.transition(version_id, reviewer, "reviewer", "approve")
        self.db.transition(version_id, reviewer, "reviewer", "publish")

    def test_same_stop_conflict_blocks_approval_and_recomputes_on_resubmit(self):
        va = self._disruption("D-A", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00")["draft_version_id"]
        self.db.add_change(va, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self._publish(va)

        vb = self._disruption("D-B", "2026-09-24T22:00:00+08:00", "2026-09-25T06:00:00+08:00", "planner-02")["draft_version_id"]
        self.db.add_change(vb, "planner-02", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self.db.transition(vb, "planner-02", "planner", "submit")
        version = self.db.get_version(vb)
        self.assertEqual(version["status"], "review")
        self.assertEqual(len(version["conflicts"]), 1)
        conflict = version["conflicts"][0]
        self.assertEqual(conflict["conflict_version_id"], va)
        self.assertEqual(conflict["reason"], "same_stop")
        self.assertEqual(conflict["detail"]["mine"]["stop_id"], self.stops["S4"])
        self.assertEqual(conflict["detail"]["theirs"]["stop_id"], self.stops["S4"])
        self.assertEqual(conflict["detail"]["conflict_disruption_code"], "D-A")
        # Conflicting versions cannot be approved or published.
        with self.assertRaises(DomainError):
            self.db.transition(vb, "reviewer-02", "reviewer", "approve")
        # Return for adjustment, then resubmit: the list is recalculated
        # from current content instead of accumulating stale entries.
        self.db.transition(vb, "reviewer-02", "reviewer", "reject")
        self.assertEqual(self.db.get_version(vb)["status"], "draft")
        self.db.add_change(vb, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S3"], "travel_minutes": 12}, "planner")
        self.db.transition(vb, "planner-02", "planner", "submit")
        conflicts = self.db.get_version(vb)["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["reason"], "same_stop")

    def test_same_detour_endpoints_conflict(self):
        va = self._disruption("D-C", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00")["draft_version_id"]
        self.db.add_change(va, "planner-01", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 18}, "planner")
        self._publish(va)

        vb = self._disruption("D-D", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00", "planner-02")["draft_version_id"]
        self.db.add_change(vb, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S5"], "to_stop_id": self.stops["S1"], "travel_minutes": 20}, "planner")
        self.db.transition(vb, "planner-02", "planner", "submit")
        conflicts = self.db.get_version(vb)["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["reason"], "same_detour")

    def test_no_conflict_when_periods_or_windows_do_not_overlap(self):
        va = self._disruption("D-E", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00")["draft_version_id"]
        self.db.add_change(va, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"], "effective_start_minute": 1200, "effective_end_minute": 1260}, "planner")
        self._publish(va)

        # Different disruption period: no conflict, flow proceeds as usual.
        vb = self._disruption("D-F", "2026-09-26T20:00:00+08:00", "2026-09-27T02:00:00+08:00", "planner-02")["draft_version_id"]
        self.db.add_change(vb, "planner-02", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self._publish(vb, "planner-02", "reviewer-02")
        self.assertEqual(self.db.get_version(vb)["status"], "published")

        # Overlapping period but disjoint effective windows: no conflict.
        vc = self._disruption("D-G", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00", "planner-02")["draft_version_id"]
        self.db.add_change(vc, "planner-02", {"kind": "stop_closure", "stop_id": self.stops["S4"], "effective_start_minute": 1260, "effective_end_minute": 1320}, "planner")
        self._publish(vc, "planner-02", "reviewer-02")
        self.assertEqual(self.db.get_version(vc)["status"], "published")

    def test_publish_rechecks_conflicts_against_newly_published_plans(self):
        va = self._disruption("D-H", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00")["draft_version_id"]
        self.db.add_change(va, "planner-01", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")

        # B is submitted and approved while A is still unpublished: no conflict yet.
        vb = self._disruption("D-I", "2026-09-24T20:00:00+08:00", "2026-09-25T02:00:00+08:00", "planner-02")["draft_version_id"]
        self.db.add_change(vb, "planner-02", {"kind": "stop_closure", "stop_id": self.stops["S4"]}, "planner")
        self.db.transition(vb, "planner-02", "planner", "submit")
        self.assertEqual(self.db.get_version(vb)["conflicts"], [])
        self.db.transition(vb, "reviewer-02", "reviewer", "approve")

        # A gets published first; B's publish must be re-checked and blocked.
        self._publish(va)
        with self.assertRaises(DomainError):
            self.db.transition(vb, "reviewer-02", "reviewer", "publish")
        self.assertEqual(self.db.get_version(vb)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
