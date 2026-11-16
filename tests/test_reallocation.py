from copy import deepcopy

from fastapi.testclient import TestClient

from carbon_trace.api import create_app
from common import Scenario


P2 = {"plant_id": "P2", "name": "Sibling Plant", "timezone": "Europe/Berlin",
      "active_from": "2026-01-01"}
LINE2 = {"line_id": "L2", "plant_id": "P2", "daily_capacity": "80",
         "active_from": "2026-01-01"}
ORDER = {"order_id": "O1", "plant_id": "P1", "grade": "G1",
         "due_day": "2026-02-06", "quantity": "50", "destination": "Buyer"}
PLAN_ITEM = {"order_id": "O1", "supplier_plant_id": "P2",
             "supply_local_day": "2026-02-05", "quantity": "40"}
WINDOW = {"from_day": "2026-02-01", "to_day": "2026-02-10"}


class ReallocationTests(Scenario):
    def two_plants(self):
        self.catalog()
        self.app.register_plant(deepcopy(P2))
        self.app.register_line(deepcopy(LINE2))
        self.app.create_order(deepcopy(ORDER))

    def make_plan(self, plan_id="PL1", items=None, expected_revision=None, **overrides):
        payload = {"plan_id": plan_id, "requesting_plant_id": "P1", **WINDOW,
                   "expected_revision": (self.app.status()["revision"]
                                         if expected_revision is None else expected_revision),
                   "items": [deepcopy(PLAN_ITEM)] if items is None else items}
        payload.update(overrides)
        return self.app.create_reallocation(payload)

    # ------------------------------------------------------------------ create
    def test_plan_creation_is_withdrawable_and_does_not_touch_facts(self):
        self.two_plants()
        result = self.make_plan()
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["total_tons"], "40")
        view = self.app.reallocation.get("PL1")
        self.assertFalse(view["stale"])
        self.assertEqual(view["staleness_reasons"], [])
        order = self.app.orders.detail("O1")
        self.assertEqual(order["reserved_tons"], "0")
        self.assertEqual(order["reservations"], [])
        revision = self.app.status()["revision"]
        self.app.reallocation.withdraw("PL1", {"expected_revision": revision})
        self.assertEqual(self.app.reallocation.get("PL1")["status"], "withdrawn")

    def test_same_spare_margin_cannot_be_promised_twice(self):
        self.two_plants()
        self.make_plan()
        self.app.create_order({**ORDER, "order_id": "O2"})
        second = {"order_id": "O2", "supplier_plant_id": "P2",
                  "supply_local_day": "2026-02-05", "quantity": "41"}
        self.expect_domain(lambda: self.make_plan("PL2", items=[second]), "business_invariant")
        # the failed attempt leaves nothing behind
        self.assertEqual(len(self.app.reallocation.list_plans()["plans"]), 1)

    def test_withdrawn_plan_releases_spare_margin(self):
        self.two_plants()
        self.make_plan()
        self.app.reallocation.withdraw("PL1", {"expected_revision": self.app.status()["revision"]})
        self.make_plan("PL2")
        spare = self.app.reallocation.spare_capacity("P2", "2026-02-05", "2026-02-05")["rows"][0]
        self.assertEqual(spare["committed_tons"], "40")
        self.assertEqual(spare["available_tons"], "40")

    def test_stale_plan_stops_occupying_spare_margin(self):
        self.two_plants()
        self.make_plan()
        self.app.open_incident({"incident_id": "I1", "line_id": "L2",
                                "started_at": "2026-02-05T05:00:00Z",
                                "severity": "major", "reason": "Outage"})
        view = self.app.reallocation.get("PL1")
        self.assertTrue(view["stale"])
        self.assertEqual(view["staleness_reasons"], ["incidents_changed"])
        spare = self.app.reallocation.spare_capacity("P2", "2026-02-05", "2026-02-05")["rows"][0]
        self.assertEqual(spare["committed_tons"], "0")
        self.make_plan("PL2")

    def test_stale_revision_guard_on_create_and_withdraw(self):
        self.two_plants()
        self.expect_domain(lambda: self.make_plan(expected_revision=0), "stale_revision")
        self.make_plan()
        self.expect_domain(
            lambda: self.app.reallocation.withdraw("PL1", {"expected_revision": 0}),
            "stale_revision")

    def test_supplier_must_differ_from_requesting_plant(self):
        self.two_plants()
        item = {**PLAN_ITEM, "supplier_plant_id": "P1"}
        self.expect_domain(lambda: self.make_plan(items=[item]), "business_invariant")

    def test_plan_cannot_exceed_outstanding_order_quantity(self):
        self.two_plants()
        item = {**PLAN_ITEM, "quantity": "51"}
        self.expect_domain(lambda: self.make_plan(items=[item]), "business_invariant")

    def test_order_plant_scope_is_enforced(self):
        self.two_plants()
        self.app.create_order({**ORDER, "order_id": "O2", "plant_id": "P2"})
        item = {**PLAN_ITEM, "order_id": "O2"}
        self.expect_domain(lambda: self.make_plan(items=[item]), "business_invariant")

    def test_unknown_grade_specification_on_supply_day_rejected(self):
        self.two_plants()
        # G3 has a standard at the order due day (so the order can exist) but
        # none covering the supplier's earlier supply day.
        self.app.register_specification({"spec_id": "S3", "grade": "G3", "valid_from": "2026-02-06",
                                         "sulfur_min": "0", "sulfur_max": "1", "ash_max": "1"})
        self.app.create_order({**ORDER, "order_id": "O3", "grade": "G3", "plant_id": "P1"})
        item = {"order_id": "O3", "supplier_plant_id": "P2",
                "supply_local_day": "2026-02-05", "quantity": "10"}
        self.expect_domain(lambda: self.make_plan(items=[item]), "not_found")

    def test_duplicate_plan_id_conflicts(self):
        self.two_plants()
        self.make_plan()
        self.expect_domain(lambda: self.make_plan(), "conflict")

    def test_withdraw_twice_conflicts(self):
        self.two_plants()
        self.make_plan()
        self.app.reallocation.withdraw("PL1", {"expected_revision": self.app.status()["revision"]})
        self.expect_domain(
            lambda: self.app.reallocation.withdraw(
                "PL1", {"expected_revision": self.app.status()["revision"]}), "conflict")

    def test_validation_failures_leave_no_trace(self):
        self.two_plants()
        revision_before = self.app.status()["revision"]
        self.expect_domain(
            lambda: self.app.create_reallocation(
                {"plan_id": "PBAD", "requesting_plant_id": "P1", **WINDOW,
                 "expected_revision": revision_before, "items": []}), "invalid_input")
        self.expect_domain(
            lambda: self.app.create_reallocation(
                {"plan_id": "PBAD", "requesting_plant_id": "P1",
                 "from_day": "2026-02-10", "to_day": "2026-02-01",
                 "expected_revision": revision_before, "items": [PLAN_ITEM]}), "invalid_input")
        outside = {**PLAN_ITEM, "supply_local_day": "2026-02-12"}
        self.expect_domain(
            lambda: self.app.create_reallocation(
                {"plan_id": "PBAD", "requesting_plant_id": "P1", **WINDOW,
                 "expected_revision": revision_before, "items": [outside]}), "invalid_input")
        self.assertEqual(self.app.status()["revision"], revision_before)
        self.assertEqual(self.app.reallocation.list_plans()["plans"], [])

    def test_unknown_plant_and_order_return_not_found(self):
        self.two_plants()
        revision = self.app.status()["revision"]
        self.expect_domain(
            lambda: self.app.create_reallocation(
                {"plan_id": "PX", "requesting_plant_id": "NOPE", **WINDOW,
                 "expected_revision": revision, "items": [PLAN_ITEM]}), "not_found")
        missing_order = {**PLAN_ITEM, "order_id": "GHOST"}
        self.expect_domain(
            lambda: self.make_plan(items=[missing_order]), "not_found")

    def test_plan_on_inactive_supplier_day_rejected(self):
        self.two_plants()
        self.app.register_plant({"plant_id": "P3", "name": "Retired", "timezone": "UTC",
                                 "active_from": "2026-01-01", "active_to": "2026-02-01"})
        self.app.register_line({"line_id": "L3", "plant_id": "P3", "daily_capacity": "10",
                                "active_from": "2026-01-01", "active_to": "2026-02-01"})
        item = {**PLAN_ITEM, "supplier_plant_id": "P3"}
        self.expect_domain(lambda: self.make_plan(items=[item]), "business_invariant")

    def test_order_due_day_must_lie_inside_plan_window(self):
        self.two_plants()
        self.app.create_order({**ORDER, "order_id": "O4", "due_day": "2026-02-20"})
        item = {**PLAN_ITEM, "order_id": "O4"}
        self.expect_domain(lambda: self.make_plan(items=[item]), "invalid_input")

    # --------------------------------------------------------------- staleness
    def test_order_cancellation_exposes_expired_plan(self):
        self.two_plants()
        self.make_plan()
        self.app.cancel_order("O1", {"expected_revision": self.app.status()["revision"]})
        reasons = self.app.evaluate_reallocation("PL1")["staleness_reasons"]
        self.assertIn("order_cancelled", reasons)
        self.assertIn("orders_changed", reasons)

    def test_recorded_production_shrinking_margin_marks_plan_stale(self):
        self.two_plants()
        self.make_plan()
        self.ingest({"kind": "lot", "external_id": "lot-9", "lot_id": "LOT9", "line_id": "L2",
                     "grade": "G1", "produced_at": "2026-02-05T02:00:00Z", "quantity": "50"},
                    batch_id="B9")
        reasons = self.app.evaluate_reallocation("PL1")["staleness_reasons"]
        self.assertIn("capacity_basis_changed", reasons)
        self.assertIn("spare_capacity_no_longer_available", reasons)

    def test_reservation_reducing_outstanding_marks_plan_stale(self):
        self.two_plants()
        self.make_plan()
        self.ingest({"kind": "lot", "external_id": "lot-8", "lot_id": "LOT8", "line_id": "L1",
                     "grade": "G1", "produced_at": "2026-02-02T02:00:00Z", "quantity": "45"},
                    {"kind": "assay", "external_id": "assay-8", "assay_id": "A8", "lot_id": "LOT8",
                     "sampled_at": "2026-02-02T03:00:00Z", "sulfur": "0.7", "ash": "0.4"},
                    batch_id="B8")
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT8",
                          "quantity": "20", "expected_revision": self.app.status()["revision"]})
        reasons = self.app.evaluate_reallocation("PL1")["staleness_reasons"]
        self.assertIn("order_requirement_changed", reasons)

    def test_closed_incident_keeps_history_but_changes_basis(self):
        self.two_plants()
        self.make_plan()
        self.app.open_incident({"incident_id": "I1", "line_id": "L2",
                                "started_at": "2026-02-05T05:00:00Z",
                                "severity": "minor", "reason": "Check"})
        self.assertEqual(self.app.reallocation.get("PL1")["staleness_reasons"], ["incidents_changed"])
        self.app.incidents.close("I1", "2026-02-05T08:00:00Z", self.app.status()["revision"])
        self.assertEqual(self.app.reallocation.get("PL1")["staleness_reasons"], ["incidents_changed"])

    # -------------------------------------------------------------- evaluation
    def test_evaluation_reports_local_baseline_loads_and_unfulfilled(self):
        self.two_plants()
        self.make_plan()
        ev = self.app.evaluate_reallocation("PL1")
        self.assertFalse(ev["stale"])
        self.assertEqual(ev["local_fulfilment"]["plant_id"], "P1")
        self.assertEqual(ev["local_fulfilment"]["total_due_tons"], "50")
        self.assertEqual(ev["totals"]["reallocated_tons"], "40")
        self.assertEqual(ev["totals"]["unfulfilled_tons"], "10")
        self.assertEqual(ev["totals"]["on_time_tons"], "40")
        self.assertEqual(ev["totals"]["late_tons"], "0")
        self.assertEqual(len(ev["unfulfilled"]), 1)
        self.assertEqual(ev["unfulfilled"][0]["unfilled_tons"], "10")
        load = ev["plant_loads"][0]
        self.assertEqual(load["plant_id"], "P2")
        self.assertEqual(load["committed_tons"], "40")
        self.assertEqual(load["by_day"][0]["local_day"], "2026-02-05")

    def test_evaluation_carries_explicit_date_caliber(self):
        self.two_plants()
        self.make_plan()
        ev = self.app.evaluate_reallocation("PL1")
        basis = ev["date_basis"]
        self.assertIn("on_time_rule", basis)
        self.assertEqual(basis["plants"]["P1"]["timezone"], "Asia/Shanghai")
        self.assertEqual(basis["plants"]["P2"]["timezone"], "Europe/Berlin")
        item = ev["items"][0]
        self.assertEqual(item["supply_day_start_utc"], "2026-02-04T23:00:00Z")
        self.assertEqual(item["due_day_start_utc"], "2026-02-05T16:00:00Z")
        self.assertTrue(item["on_time"])

    def test_late_supply_day_is_flagged(self):
        self.two_plants()
        item = {**PLAN_ITEM, "supply_local_day": "2026-02-08"}
        self.make_plan(items=[item])
        item_view = self.app.evaluate_reallocation("PL1")["items"][0]
        self.assertFalse(item_view["on_time"])
        ev = self.app.evaluate_reallocation("PL1")
        self.assertEqual(ev["totals"]["late_tons"], "40")
        self.assertEqual(ev["totals"]["on_time_tons"], "0")

    def test_spare_capacity_uses_plant_local_calendar(self):
        self.two_plants()
        rows = self.app.reallocation.spare_capacity("P2", "2026-02-05", "2026-02-06")["rows"]
        self.assertEqual([row["local_day"] for row in rows], ["2026-02-05", "2026-02-06"])
        self.assertEqual(rows[0]["capacity_tons"], "80")
        self.assertEqual(rows[0]["spare_tons"], "80")
        self.assertEqual(rows[0]["day_start_utc"], "2026-02-04T23:00:00Z")

    def test_recorded_production_reduces_nominal_spare(self):
        self.two_plants()
        self.ingest({"kind": "lot", "external_id": "lot-2", "lot_id": "LOT2", "line_id": "L2",
                     "grade": "G1", "produced_at": "2026-02-05T02:00:00Z", "quantity": "55"},
                    batch_id="B2")
        row = self.app.reallocation.spare_capacity("P2", "2026-02-05", "2026-02-05")["rows"][0]
        self.assertEqual(row["spare_tons"], "25")
        self.expect_domain(lambda: self.make_plan(items=[{**PLAN_ITEM, "quantity": "26"}]),
                           "business_invariant")
        self.make_plan(items=[{**PLAN_ITEM, "quantity": "25"}])

    def test_multiple_plans_share_spare_without_overdraft(self):
        self.two_plants()
        self.app.create_order({**ORDER, "order_id": "O2"})
        self.make_plan("PL1", items=[{**PLAN_ITEM, "order_id": "O1", "quantity": "30"}])
        self.make_plan("PL2", items=[{**PLAN_ITEM, "order_id": "O2", "quantity": "30"}])
        self.app.create_order({**ORDER, "order_id": "O3"})
        self.expect_domain(
            lambda: self.make_plan(
                "PL3", items=[{**PLAN_ITEM, "order_id": "O3", "quantity": "21"}]),
            "business_invariant")
        row = self.app.reallocation.spare_capacity("P2", "2026-02-05", "2026-02-05")["rows"][0]
        self.assertEqual(row["committed_tons"], "60")
        self.assertEqual(row["available_tons"], "20")

    # ------------------------------------------------------ reporting isolation
    def test_plans_do_not_change_daily_report_or_publication(self):
        self.two_plants()
        self.make_plan()
        report = self.app.report("P1", "2026-02-01", "2026-02-10")
        self.assertTrue(all(set(row) >= {"capacity_tons", "production_tons", "shipped_tons"}
                            for row in report["rows"]))
        self.assertEqual(report["summary"]["production_tons"], "0")
        revision = self.app.status()["revision"]
        self.app.prepare({"candidate_id": "V1", "plant_id": "P1", **WINDOW,
                          "expected_head": 0, "expected_revision": revision})
        published = self.app.publish("V1", {"expected_head": 0, "expected_revision": revision})
        frozen = self.app.publication.version(published["version"])
        self.assertNotIn("reallocation", str(frozen["rows"]).lower())

    def test_trace_resolves_plan_audit_entries(self):
        self.two_plants()
        self.make_plan()
        timeline = self.app.trace.timeline(1, self.app.status()["revision"])
        causes = {row["cause"] for row in timeline}
        self.assertIn("reallocation_plan", causes)
        entry = next(row for row in timeline if row["cause"] == "reallocation_plan")
        resolved = self.app.trace.revision(entry["revision"])
        self.assertEqual(resolved["entity"]["plan_id"], "PL1")

    # --------------------------------------------------------------------- api
    def test_api_workflow(self):
        self.two_plants()
        client = TestClient(create_app(self.path))
        create = client.post("/reallocations", json={
            "plan_id": "PL1", "requesting_plant_id": "P1", **WINDOW,
            "expected_revision": self.app.status()["revision"],
            "items": [PLAN_ITEM]})
        self.assertEqual(create.status_code, 201)
        self.assertEqual(client.get("/reallocations").json()["plans"][0]["plan_id"], "PL1")
        self.assertEqual(client.get("/reallocations/PL1").status_code, 200)
        evaluation = client.get("/reallocations/PL1/evaluation")
        self.assertEqual(evaluation.status_code, 200)
        self.assertEqual(evaluation.json()["totals"]["unfulfilled_tons"], "10")
        spare = client.get("/plants/P2/spare-capacity",
                           params={"from_day": "2026-02-05", "to_day": "2026-02-05"})
        self.assertEqual(spare.json()["rows"][0]["available_tons"], "40")
        self.app.create_order({**ORDER, "order_id": "O2"})
        double = client.post("/reallocations", json={
            "plan_id": "PL2", "requesting_plant_id": "P1", **WINDOW,
            "expected_revision": self.app.status()["revision"],
            "items": [{"order_id": "O2", "supplier_plant_id": "P2",
                       "supply_local_day": "2026-02-05", "quantity": "41"}]})
        self.assertEqual(double.status_code, 409)
        withdraw = client.post("/reallocations/PL1/withdraw",
                               json={"expected_revision": self.app.status()["revision"]})
        self.assertEqual(withdraw.status_code, 201)
        self.assertEqual(client.get("/reallocations?status=active").json()["plans"], [])
        self.assertEqual(len(client.get("/reallocations?status=withdrawn").json()["plans"]), 1)
