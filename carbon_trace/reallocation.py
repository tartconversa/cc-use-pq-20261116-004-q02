from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .catalog import Catalog
from .errors import Conflict, Invariant, Stale, require
from .forecast import Forecast
from .orders import Orders
from .store import Store
from .values import as_number, day, decimal, digest, identifier, mapping, text


class Reallocation:
    """Withdrawable cross-plant capacity reallocation plans.

    Plans form a separate planning layer: they never mutate lots, shipments,
    orders, reservations or the published daily reports. A plan freezes a
    digest of the facts it relies on (open orders, incidents, per-plant local
    spare capacity); when those facts move, the plan is reported as stale on
    read instead of being silently rewritten.
    """

    MAX_ITEMS = 200
    MAX_WINDOW_DAYS = 366

    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ create
    def create(self, payload: dict) -> dict:
        payload = mapping(payload)
        plan_id = identifier(payload.get("plan_id"), "plan_id")
        requesting = identifier(payload.get("requesting_plant_id"), "requesting_plant_id")
        from_day = day(payload.get("from_day"), "from_day")
        to_day = day(payload.get("to_day"), "to_day")
        require(from_day <= to_day, "date interval is inverted")
        require((date.fromisoformat(to_day) - date.fromisoformat(from_day)).days <= self.MAX_WINDOW_DAYS,
                "date interval exceeds 366 days")
        note = payload.get("note")
        if note is not None:
            note = text(note, "note", max_length=400)
        expected_revision = payload.get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0, "invalid expected_revision")
        raw_items = payload.get("items")
        require(isinstance(raw_items, list) and 0 < len(raw_items) <= self.MAX_ITEMS,
                f"items must contain 1-{self.MAX_ITEMS} entries", field="items")
        with self.store.write() as db:
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed before plan creation")
            if db.execute("SELECT 1 FROM reallocation_plans WHERE plan_id=?", (plan_id,)).fetchone():
                raise Conflict("plan_id already exists")
            request_plant = Catalog.plant(db, requesting)
            parsed = []
            for raw in raw_items:
                parsed.append(self._parse_item(db, raw, request_plant, from_day, to_day))
            involved = {requesting} | {item["supplier_plant_id"] for item in parsed}
            self._check_order_room(db, parsed)
            self._check_spare_room(db, plan_id, parsed)
            orders_digest, incidents_digest, capacity_digest = self._basis_digests(
                db, involved, from_day, to_day, {item["order_id"] for item in parsed})
            basis_digest = digest([orders_digest, incidents_digest, capacity_digest])
            revision = Store.advance(db, "reallocation_plan", plan_id)
            db.execute("INSERT INTO reallocation_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, requesting, from_day, to_day, note, "active",
                        expected_revision, revision, None, basis_digest, orders_digest,
                        incidents_digest, capacity_digest))
            db.executemany("INSERT INTO reallocation_items VALUES (?,?,?,?,?,?,?)",
                           [(plan_id, seq, item["order_id"], item["supplier_plant_id"],
                             item["grade"], item["supply_local_day"], as_number(item["quantity"]))
                            for seq, item in enumerate(parsed)])
            return {"plan_id": plan_id, "revision": revision, "status": "active",
                    "item_count": len(parsed),
                    "total_tons": as_number(sum((item["quantity"] for item in parsed), Decimal(0))),
                    "basis_digest": basis_digest}

    def _parse_item(self, db, raw: dict, request_plant, from_day: str, to_day: str) -> dict:
        item = mapping(raw, "item")
        order_id = identifier(item.get("order_id"), "order_id")
        supplier_id = identifier(item.get("supplier_plant_id"), "supplier_plant_id")
        supply_day = day(item.get("supply_local_day"), "supply_local_day")
        quantity = decimal(item.get("quantity"), "quantity", positive=True)
        require(from_day <= supply_day <= to_day, "supply_local_day outside plan window")
        if supplier_id == request_plant["plant_id"]:
            raise Invariant("reallocation supplier must differ from requesting plant")
        order = Store.one(db, "SELECT * FROM orders WHERE order_id=?", (order_id,), "order")
        if order["status"] != "open":
            raise Invariant("order is not open")
        if order["plant_id"] != request_plant["plant_id"]:
            raise Invariant("order does not belong to requesting plant")
        require(from_day <= order["due_day"] <= to_day,
                "order due_day must fall inside the plan window")
        supplier = Catalog.plant(db, supplier_id)
        if not Catalog.valid_on(supplier["active_from"], supplier["active_to"], supply_day):
            raise Invariant("supplier plant inactive on supply day")
        lines = db.execute("SELECT * FROM lines WHERE plant_id=?", (supplier_id,)).fetchall()
        active_lines = [line for line in lines
                        if Catalog.valid_on(line["active_from"], line["active_to"], supply_day)]
        if not active_lines:
            raise Invariant("supplier has no active line on supply day")
        Catalog.specification(db, order["grade"], supply_day)
        return {"order_id": order_id, "supplier_plant_id": supplier_id,
                "grade": order["grade"], "supply_local_day": supply_day, "quantity": quantity}

    @staticmethod
    def _outstanding(db, order_row) -> Decimal:
        reserved = Orders._sum(
            db, "SELECT quantity FROM reservations WHERE order_id=? AND status='active'",
            order_row["order_id"])
        return decimal(order_row["quantity"], "quantity") - reserved

    def _check_order_room(self, db, items: list[dict]) -> None:
        wanted = defaultdict(lambda: Decimal(0))
        for item in items:
            wanted[item["order_id"]] += item["quantity"]
        for order_id, quantity in wanted.items():
            order = Store.one(db, "SELECT * FROM orders WHERE order_id=?", (order_id,), "order")
            if quantity > self._outstanding(db, order):
                raise Invariant("plan exceeds outstanding order quantity",
                                detail={"order_id": order_id})

    def _check_spare_room(self, db, plan_id: str, items: list[dict]) -> None:
        requested = defaultdict(lambda: Decimal(0))
        days_by_plant = defaultdict(set)
        for item in items:
            key = (item["supplier_plant_id"], item["supply_local_day"])
            requested[key] += item["quantity"]
            days_by_plant[item["supplier_plant_id"]].add(item["supply_local_day"])
        spare = {}
        for supplier_id, days_used in days_by_plant.items():
            plant = Catalog.plant(db, supplier_id)
            spare.update({(supplier_id, row_day): row for row_day, row
                          in self._spare_by_day(db, plant, min(days_used), max(days_used)).items()})
        committed = self._committed_spare(db, days_by_plant, exclude_plan=plan_id)
        for key, quantity in requested.items():
            already = committed.get(key, Decimal(0))
            free = spare[key]["spare"] - already
            if quantity > free:
                raise Invariant("plant spare capacity already promised to another plan",
                                detail={"plant_id": key[0], "local_day": key[1],
                                        "spare_tons": as_number(spare[key]["spare"]),
                                        "committed_tons": as_number(already),
                                        "requested_tons": as_number(quantity)})

    # ---------------------------------------------------------------- withdraw
    def withdraw(self, plan_id: str, payload: dict) -> dict:
        plan_id = identifier(plan_id, "plan_id")
        expected_revision = mapping(payload).get("expected_revision")
        require(type(expected_revision) is int and expected_revision >= 0, "invalid expected_revision")
        with self.store.write() as db:
            if Store.revision(db) != expected_revision:
                raise Stale("source revision changed before withdrawal")
            plan = Store.one(db, "SELECT * FROM reallocation_plans WHERE plan_id=?", (plan_id,), "plan")
            if plan["status"] != "active":
                raise Conflict("plan already withdrawn")
            revision = Store.advance(db, "reallocation_withdraw", plan_id)
            db.execute("UPDATE reallocation_plans SET status='withdrawn',withdrawn_revision=? "
                       "WHERE plan_id=?", (revision, plan_id))
            return {"plan_id": plan_id, "revision": revision, "status": "withdrawn"}

    # -------------------------------------------------------------------- read
    def get(self, plan_id: str) -> dict:
        plan_id = identifier(plan_id, "plan_id")
        with self.store.read() as db:
            plan = Store.one(db, "SELECT * FROM reallocation_plans WHERE plan_id=?", (plan_id,), "plan")
            return self._plan_view(db, plan)

    def list_plans(self, status_filter: str | None = None) -> dict:
        require(status_filter in (None, "active", "withdrawn"), "invalid status filter")
        with self.store.read() as db:
            sql = "SELECT * FROM reallocation_plans"
            params = ()
            if status_filter:
                sql += " WHERE status=?"
                params = (status_filter,)
            sql += " ORDER BY created_revision,plan_id"
            plans = db.execute(sql, params).fetchall()
            return {"source_revision": Store.revision(db),
                    "plans": [self._plan_view(db, plan) for plan in plans]}

    def _plan_view(self, db, plan) -> dict:
        items = [dict(row) for row in db.execute(
            "SELECT * FROM reallocation_items WHERE plan_id=? ORDER BY item_seq", (plan["plan_id"],))]
        freshness = self._freshness(db, plan)
        total = sum((decimal(row["quantity"], "quantity") for row in items), Decimal(0))
        return {"plan_id": plan["plan_id"], "status": plan["status"],
                "requesting_plant_id": plan["requesting_plant_id"],
                "from_day": plan["from_day"], "to_day": plan["to_day"],
                "note": plan["note"], "base_revision": plan["base_revision"],
                "created_revision": plan["created_revision"],
                "withdrawn_revision": plan["withdrawn_revision"],
                "stale": not freshness["fresh"], "staleness_reasons": freshness["reasons"],
                "item_count": len(items), "total_tons": as_number(total),
                "source_revision": Store.revision(db)}

    def evaluate(self, plan_id: str) -> dict:
        plan_id = identifier(plan_id, "plan_id")
        with self.store.read() as db:
            plan = Store.one(db, "SELECT * FROM reallocation_plans WHERE plan_id=?", (plan_id,), "plan")
            items = [dict(row) for row in db.execute(
                "SELECT * FROM reallocation_items WHERE plan_id=? ORDER BY item_seq",
                (plan_id,))]
            request_plant = Catalog.plant(db, plan["requesting_plant_id"])
            suppliers = {row["supplier_plant_id"]: Catalog.plant(db, row["supplier_plant_id"])
                         for row in items}
            freshness = self._freshness(db, plan, items=items)
            baseline, due_totals, backlog = Forecast.projection(
                db, request_plant, plan["from_day"], plan["to_day"])
            item_views, plant_loads, on_time, late = self._item_views(
                db, items, request_plant, suppliers)
            unfulfilled, totals = self._unfulfilled(db, plan, items, request_plant)
            date_basis = self._date_basis(request_plant, suppliers, plan)
            return {"plan_id": plan_id, "status": plan["status"],
                    "stale": not freshness["fresh"], "staleness_reasons": freshness["reasons"],
                    "base_revision": plan["base_revision"],
                    "created_revision": plan["created_revision"],
                    "withdrawn_revision": plan["withdrawn_revision"],
                    "source_revision": Store.revision(db),
                    "requesting_plant_id": plan["requesting_plant_id"],
                    "from_day": plan["from_day"], "to_day": plan["to_day"],
                    "date_basis": date_basis,
                    "local_fulfilment": {"plant_id": plan["requesting_plant_id"],
                                         "source_revision": Store.revision(db),
                                         "rows": baseline,
                                         "ending_backlog_tons": as_number(backlog),
                                         "total_due_tons": as_number(sum(due_totals.values(), Decimal(0)))},
                    "plant_loads": plant_loads, "items": item_views,
                    "unfulfilled": unfulfilled,
                    "totals": {**totals, "on_time_tons": as_number(on_time),
                               "late_tons": as_number(late)}}

    def spare_capacity(self, plant_id: str, from_day: str, to_day: str) -> dict:
        plant_id = identifier(plant_id, "plant_id")
        from_day = day(from_day, "from_day")
        to_day = day(to_day, "to_day")
        require(from_day <= to_day, "date interval is inverted")
        require((date.fromisoformat(to_day) - date.fromisoformat(from_day)).days <= self.MAX_WINDOW_DAYS,
                "date interval exceeds 366 days")
        with self.store.read() as db:
            plant = Catalog.plant(db, plant_id)
            spare = self._spare_by_day(db, plant, from_day, to_day)
            committed = self._committed_spare(db, {plant_id: set(spare)}, exclude_plan=None)
            rows = []
            for local_day in sorted(spare):
                promised = committed.get((plant_id, local_day), Decimal(0))
                entry = spare[local_day]
                rows.append({"local_day": local_day,
                             "day_start_utc": self.day_start_utc(local_day, plant["timezone"]),
                             "capacity_tons": as_number(entry["capacity"]),
                             "recorded_production_tons": as_number(entry["recorded"]),
                             "spare_tons": as_number(entry["spare"]),
                             "committed_tons": as_number(promised),
                             "available_tons": as_number(entry["spare"] - promised)})
            return {"plant_id": plant_id, "timezone": plant["timezone"],
                    "source_revision": Store.revision(db),
                    "commitment_rule": "only active and fresh reallocation plans promise spare capacity",
                    "rows": rows}

    # ------------------------------------------------------------- freshness
    def _freshness(self, db, plan, items: list[dict] | None = None) -> dict:
        if items is None:
            items = [dict(row) for row in db.execute(
                "SELECT * FROM reallocation_items WHERE plan_id=?", (plan["plan_id"],))]
        reasons = self._digest_reasons(db, plan, items)
        self._spare_violations(db, plan, items, reasons)
        return {"fresh": not reasons, "reasons": reasons}

    def _digest_reasons(self, db, plan, items: list[dict]) -> list[str]:
        """Staleness derivable without re-checking promised spare capacity."""
        involved = {plan["requesting_plant_id"]} | {row["supplier_plant_id"] for row in items}
        orders_digest, incidents_digest, capacity_digest = self._basis_digests(
            db, involved, plan["from_day"], plan["to_day"],
            {row["order_id"] for row in items})
        reasons = []
        if orders_digest != plan["orders_digest"]:
            reasons.append("orders_changed")
        if incidents_digest != plan["incidents_digest"]:
            reasons.append("incidents_changed")
        if capacity_digest != plan["capacity_digest"]:
            reasons.append("capacity_basis_changed")
        order_wanted = defaultdict(lambda: Decimal(0))
        for row in items:
            order_wanted[row["order_id"]] += decimal(row["quantity"], "quantity")
        cancelled = False
        for order_id, wanted in order_wanted.items():
            order = db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if order is None or order["status"] != "open":
                cancelled = True
            elif wanted > self._outstanding(db, order):
                reasons.append("order_requirement_changed")
        if cancelled:
            reasons.append("order_cancelled")
        return reasons

    def _spare_violations(self, db, plan, items: list[dict], reasons: list[str]) -> None:
        if plan["status"] != "active":
            return
        days_by_plant = defaultdict(set)
        wanted = defaultdict(lambda: Decimal(0))
        for row in items:
            days_by_plant[row["supplier_plant_id"]].add(row["supply_local_day"])
            wanted[(row["supplier_plant_id"], row["supply_local_day"])] += decimal(
                row["quantity"], "quantity")
        committed = self._committed_spare(db, days_by_plant, exclude_plan=plan["plan_id"])
        for key, quantity in wanted.items():
            supplier = Catalog.plant(db, key[0])
            spare = self._spare_by_day(db, supplier, key[1], key[1])[key[1]]["spare"]
            if quantity + committed.get(key, Decimal(0)) > spare:
                reasons.append("spare_capacity_no_longer_available")
                return

    def _basis_digests(self, db, plant_ids: set[str],
                       from_day: str, to_day: str, order_ids: set[str]):
        # Only orders the plan actually promises against belong to its order
        # basis: an unrelated new order in the window must not expire the plan.
        order_placeholders = ",".join("?" for _ in order_ids) or "''"
        order_rows = db.execute(
            f"SELECT * FROM orders WHERE order_id IN ({order_placeholders}) ORDER BY order_id",
            tuple(sorted(order_ids))).fetchall()
        orders_basis = []
        for row in order_rows:
            reserved = self._outstanding(db, row)
            orders_basis.append({"order_id": row["order_id"], "status": row["status"],
                                 "due_day": row["due_day"], "grade": row["grade"],
                                 "quantity": row["quantity"],
                                 "outstanding_tons": as_number(reserved)})
        plant_placeholders = ",".join("?" for _ in plant_ids)
        incident_rows = db.execute(
            f"SELECT * FROM incidents WHERE plant_id IN ({plant_placeholders}) "
            f"AND start_day<=? AND (end_day IS NULL OR end_day>=?) "
            f"ORDER BY incident_id", (*sorted(plant_ids), to_day, from_day)).fetchall()
        incidents_basis = [{"incident_id": row["incident_id"], "plant_id": row["plant_id"],
                            "line_id": row["line_id"], "start_day": row["start_day"],
                            "end_day": row["end_day"], "severity": row["severity"],
                            "status": row["status"]} for row in incident_rows]
        capacity_basis = []
        for plant_id in sorted(plant_ids):
            plant = Catalog.plant(db, plant_id)
            for local_day, entry in self._spare_by_day(db, plant, from_day, to_day).items():
                capacity_basis.append({"plant_id": plant_id, "local_day": local_day,
                                       "capacity_tons": as_number(entry["capacity"]),
                                       "recorded_production_tons": as_number(entry["recorded"])})
        return digest(orders_basis), digest(incidents_basis), digest(capacity_basis)

    # -------------------------------------------------------------- aggregates
    def _spare_by_day(self, db, plant, from_day: str, to_day: str) -> dict:
        lines = db.execute("SELECT * FROM lines WHERE plant_id=? ORDER BY line_id",
                           (plant["plant_id"],)).fetchall()
        recorded_rows = db.execute(
            "SELECT lots.local_day AS local_day, lots.quantity AS quantity FROM lots JOIN lines "
            "ON lines.line_id=lots.line_id WHERE lines.plant_id=? AND lots.local_day BETWEEN ? AND ?",
            (plant["plant_id"], from_day, to_day)).fetchall()
        recorded = defaultdict(lambda: Decimal(0))
        for row in recorded_rows:
            recorded[row["local_day"]] += decimal(row["quantity"], "quantity")
        result = {}
        current = date.fromisoformat(from_day)
        final = date.fromisoformat(to_day)
        while current <= final:
            label = current.isoformat()
            active = [line for line in lines
                      if Catalog.valid_on(plant["active_from"], plant["active_to"], label)
                      and Catalog.valid_on(line["active_from"], line["active_to"], label)]
            capacity = sum((decimal(line["daily_capacity"], "capacity") for line in active), Decimal(0))
            produced = recorded[label]
            result[label] = {"capacity": capacity, "recorded": produced,
                             "spare": max(Decimal(0), capacity - produced)}
            current += timedelta(days=1)
        return result

    def _committed_spare(self, db, days_by_plant: dict, exclude_plan: str | None) -> dict:
        """Spare promised by active plans that still match current facts.

        Withdrawn plans never promise anything; stale active plans surface as
        expired and stop occupying spare capacity so a replacement plan can be
        drawn against the same margin.
        """
        plans = db.execute("SELECT * FROM reallocation_plans WHERE status='active'").fetchall()
        committed = defaultdict(lambda: Decimal(0))
        for plan in plans:
            if plan["plan_id"] == exclude_plan:
                continue
            items = db.execute("SELECT * FROM reallocation_items WHERE plan_id=?",
                               (plan["plan_id"],)).fetchall()
            if not items:
                continue
            if self._digest_reasons(db, plan, items):
                continue
            for row in items:
                key = (row["supplier_plant_id"], row["supply_local_day"])
                if (row["supplier_plant_id"] in days_by_plant
                        and row["supply_local_day"] in days_by_plant[row["supplier_plant_id"]]):
                    committed[key] += decimal(row["quantity"], "quantity")
        return committed

    def _item_views(self, db, items: list[dict], request_plant, suppliers: dict):
        loads = defaultdict(lambda: {"tons": Decimal(0), "count": 0, "days": defaultdict(Decimal)})
        views = []
        on_time = Decimal(0)
        late = Decimal(0)
        for row in items:
            order = Store.one(db, "SELECT * FROM orders WHERE order_id=?", (row["order_id"],), "order")
            supplier = suppliers[row["supplier_plant_id"]]
            quantity = decimal(row["quantity"], "quantity")
            supply_utc = self.day_start_utc(row["supply_local_day"], supplier["timezone"])
            due_utc = self.day_start_utc(order["due_day"], request_plant["timezone"])
            timely = supply_utc <= due_utc
            if timely:
                on_time += quantity
            else:
                late += quantity
            load = loads[row["supplier_plant_id"]]
            load["tons"] += quantity
            load["count"] += 1
            load["days"][row["supply_local_day"]] += quantity
            views.append({"item_seq": row["item_seq"], "order_id": row["order_id"],
                          "grade": row["grade"], "supplier_plant_id": row["supplier_plant_id"],
                          "supply_local_day": row["supply_local_day"],
                          "supply_day_start_utc": supply_utc,
                          "quantity": row["quantity"], "order_due_day": order["due_day"],
                          "due_day_start_utc": due_utc, "on_time": timely,
                          "order_status": order["status"],
                          "current_outstanding_tons": as_number(self._outstanding(db, order))})
        plant_loads = [{"plant_id": plant_id, "role": "supplier",
                        "timezone": suppliers[plant_id]["timezone"],
                        "committed_tons": as_number(load["tons"]), "item_count": load["count"],
                        "by_day": [{"local_day": local_day,
                                    "day_start_utc": self.day_start_utc(
                                        local_day, suppliers[plant_id]["timezone"]),
                                    "quantity": as_number(tons)}
                                   for local_day, tons in sorted(load["days"].items())]}
                       for plant_id, load in sorted(loads.items())]
        return views, plant_loads, on_time, late

    def _unfulfilled(self, db, plan, items: list[dict], request_plant) -> tuple[list[dict], dict]:
        orders = db.execute("SELECT * FROM orders WHERE plant_id=? AND status='open' "
                            "AND due_day BETWEEN ? AND ? ORDER BY due_day,order_id",
                            (request_plant["plant_id"], plan["from_day"], plan["to_day"])).fetchall()
        reallocated = defaultdict(lambda: Decimal(0))
        for row in items:
            reallocated[row["order_id"]] += decimal(row["quantity"], "quantity")
        rows = []
        requested_total = reserved_total = moved_total = unfilled_total = Decimal(0)
        for order in orders:
            requested = decimal(order["quantity"], "quantity")
            reserved = requested - self._outstanding(db, order)
            moved = reallocated.get(order["order_id"], Decimal(0))
            unfilled = max(Decimal(0), requested - reserved - moved)
            requested_total += requested
            reserved_total += reserved
            moved_total += moved
            unfilled_total += unfilled
            if unfilled > 0:
                rows.append({"order_id": order["order_id"], "due_day": order["due_day"],
                             "grade": order["grade"], "requested_tons": as_number(requested),
                             "reserved_tons": as_number(reserved),
                             "reallocated_tons": as_number(moved),
                             "unfilled_tons": as_number(unfilled)})
        totals = {"requested_open_tons": as_number(requested_total),
                  "reserved_tons": as_number(reserved_total),
                  "reallocated_tons": as_number(moved_total),
                  "unfulfilled_tons": as_number(unfilled_total)}
        return rows, totals

    def _date_basis(self, request_plant, suppliers: dict, plan) -> dict:
        plants = {request_plant["plant_id"]: request_plant, **suppliers}
        return {
            "calendar": "every day is a local date interpreted in that plant's IANA timezone",
            "on_time_rule": "on time when the UTC start instant of the supplier local day is not "
                            "later than the UTC start instant of the requesting plant due local day",
            "plants": {plant_id: {"timezone": plant["timezone"],
                                  "window_start_utc": self.day_start_utc(plan["from_day"], plant["timezone"]),
                                  "window_end_utc": self.day_start_utc(plan["to_day"], plant["timezone"])}
                       for plant_id, plant in sorted(plants.items())}}

    @staticmethod
    def day_start_utc(local_day: str, zone: str) -> str:
        instant = datetime.combine(date.fromisoformat(local_day), time.min,
                                   tzinfo=ZoneInfo(zone))
        return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
