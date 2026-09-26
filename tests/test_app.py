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

    def _publish(self, code, name, changes, starts="2026-09-24T22:00:00+08:00", ends="2026-09-25T02:00:00+08:00"):
        disruption = self.db.create_disruption("planner-01", {"code": code, "name": name, "starts_at": starts, "ends_at": ends}, "planner")
        version = disruption["draft_version_id"]
        change_ids = [self.db.add_change(version, "planner-01", change, "planner")["id"] for change in changes]
        self.db.transition(version, "planner-01", "planner", "submit")
        self.db.transition(version, "reviewer-01", "reviewer", "approve")
        self.db.transition(version, "reviewer-01", "reviewer", "publish")
        return disruption["id"], version, change_ids

    def test_station_conflict_blocks_publish_until_adjusted(self):
        self._publish("D-100", "码头夜间停运", [{"kind": "stop_closure", "stop_id": self.stops["S4"], "effective_start_minute": 1380, "effective_end_minute": 1560}])
        draft = self.db.create_disruption("planner-02", {"code": "D-101", "name": "码头跳站", "starts_at": "2026-09-24T23:00:00+08:00", "ends_at": "2026-09-25T01:00:00+08:00"}, "planner")
        v2 = draft["draft_version_id"]
        skip = self.db.add_change(v2, "planner-02", {"kind": "skip_stop", "stop_id": self.stops["S4"], "effective_start_minute": 1400, "effective_end_minute": 1500}, "planner")

        submitted = self.db.transition(v2, "planner-02", "planner", "submit")
        self.assertEqual(submitted["status"], "review")
        conflicts = self.db.list_conflicts(v2)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["conflict_type"], "station")
        self.assertEqual(conflicts[0]["shared_stop_id"], self.stops["S4"])
        self.assertEqual(conflicts[0]["published_change_id"], 1)
        self.assertEqual(conflicts[0]["details"]["published_version"]["disruption_code"], "D-100")
        self.assertEqual(conflicts[0]["details"]["change"]["id"], skip["id"])
        self.assertEqual(self.db.get_version(v2)["conflicts"][0]["id"], conflicts[0]["id"])

        # Conflicting plans cannot be approved or published, only returned.
        with self.assertRaises(DomainError):
            self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        with self.assertRaises(DomainError):
            self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.db.transition(v2, "reviewer-02", "reviewer", "reject")
        self.assertEqual(self.db.list_conflicts(v2), [])

        # Resubmitting unchanged content recomputes the same list, no leftovers.
        self.db.transition(v2, "planner-02", "planner", "submit")
        self.assertEqual(len(self.db.list_conflicts(v2)), 1)
        self.db.transition(v2, "reviewer-02", "reviewer", "reject")

        # Adjust the draft, resubmit: the old list is gone and the flow proceeds.
        self.db.delete_change(skip["id"], "planner-02", "planner")
        self.db.add_change(v2, "planner-02", {"kind": "skip_stop", "stop_id": self.stops["S2"], "effective_start_minute": 1400, "effective_end_minute": 1500}, "planner")
        resubmitted = self.db.transition(v2, "planner-02", "planner", "submit")
        self.assertEqual(resubmitted["conflicts"], [])
        self.assertEqual(self.db.list_conflicts(v2), [])
        self.db.transition(v2, "reviewer-02", "reviewer", "approve")
        published = self.db.transition(v2, "reviewer-02", "reviewer", "publish")
        self.assertEqual(published["status"], "published")

    def test_detour_od_conflict_and_window_rules(self):
        self._publish("D-110", "北站到机场绕行", [{"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 20, "effective_start_minute": 1380, "effective_end_minute": 1560}])
        same_od = self.db.create_disruption("planner-02", {"code": "D-111", "name": "同起讫绕行", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")["draft_version_id"]
        self.db.add_change(same_od, "planner-02", {"kind": "detour", "from_stop_id": self.stops["S1"], "to_stop_id": self.stops["S5"], "travel_minutes": 25}, "planner")
        self.db.transition(same_od, "planner-02", "planner", "submit")
        conflicts = self.db.list_conflicts(same_od)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["conflict_type"], "detour_od")
        self.assertIsNone(conflicts[0]["shared_stop_id"])
        self.assertEqual(conflicts[0]["details"]["reason"], "同一绕行起讫")
        self.db.transition(same_od, "reviewer-02", "reviewer", "reject")

        # Same station but disjoint service-minute windows do not conflict.
        disjoint = self.db.create_disruption("planner-02", {"code": "D-112", "name": "早间跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")["draft_version_id"]
        self.db.add_change(disjoint, "planner-02", {"kind": "skip_stop", "stop_id": self.stops["S1"], "effective_start_minute": 600, "effective_end_minute": 700}, "planner")
        self.db.transition(disjoint, "planner-02", "planner", "submit")
        self.assertEqual(self.db.list_conflicts(disjoint), [])
        self.db.transition(disjoint, "reviewer-02", "reviewer", "reject")

        # An all-day change overlaps any window of a published change on the same stop.
        allday = self.db.create_disruption("planner-02", {"code": "D-113", "name": "全天跳站", "starts_at": "2026-09-24T22:00:00+08:00", "ends_at": "2026-09-25T02:00:00+08:00"}, "planner")["draft_version_id"]
        self.db.add_change(allday, "planner-02", {"kind": "skip_stop", "stop_id": self.stops["S1"]}, "planner")
        self.db.transition(allday, "planner-02", "planner", "submit")
        self.assertEqual(len(self.db.list_conflicts(allday)), 1)

    def test_disjoint_disruption_dates_do_not_conflict(self):
        self._publish("D-120", "会展中心停运", [{"kind": "stop_closure", "stop_id": self.stops["X1"]}])
        later = self.db.create_disruption("planner-02", {"code": "D-121", "name": "下周会展中心停运", "starts_at": "2026-10-01T00:00:00+08:00", "ends_at": "2026-10-02T00:00:00+08:00"}, "planner")["draft_version_id"]
        self.db.add_change(later, "planner-02", {"kind": "stop_closure", "stop_id": self.stops["X1"]}, "planner")
        self.db.transition(later, "planner-02", "planner", "submit")
        self.assertEqual(self.db.list_conflicts(later), [])
        self.db.transition(later, "reviewer-02", "reviewer", "approve")
        self.assertEqual(self.db.transition(later, "reviewer-02", "reviewer", "publish")["status"], "published")


if __name__ == "__main__":
    unittest.main()
