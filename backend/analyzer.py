"""
analyzer.py - the "brain" of the application (no Flask, no SQL in here).

Syllabus concepts shown here:
  * Classes, inheritance (ElectricalDevice -> Appliance / Machine), polymorphism, associations
  * Lists, tuples, sets, dictionaries, maps (map/filter/zip/enumerate/sorted/max/min)
  * Lambda functions, generator (yield), recursion, Trie, Stack
  * Regular expressions (pattern matching), random numbers
  * NumPy (arrays, mean, std, polyfit), SciPy (z-score), NetworkX (device graph)
"""
import re
import random

import numpy as np
from scipy import stats
import networkx as nx

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------
# Default tariff slabs. These are APPLICATION DEFAULTS, not an official electricity-board tariff.
# "min"/"max" are for display; the telescopic calculation uses the width between successive
# "max" values (50, 50, 50, 50, 50, rest), so there is no gap between 50 and 51.
DEFAULT_TARIFF = [
    {"min": 0,   "max": 50,   "rate": 3.35},
    {"min": 51,  "max": 100,  "rate": 4.25},
    {"min": 101, "max": 150,  "rate": 5.35},
    {"min": 151, "max": 200,  "rate": 7.20},
    {"min": 201, "max": 250,  "rate": 8.50},
    {"min": 251, "max": None, "rate": 9.00},     # above 250 units (configurable default)
]

HOUSE_CATEGORIES = ["Lighting", "Fan", "Air Conditioner", "Refrigerator", "Television",
                    "Washing Machine", "Water Heater", "Microwave", "Computer",
                    "Water Pump", "Iron", "Other"]
FACTORY_TYPES = ["CNC Machine", "Lathe", "Compressor", "Industrial Fan", "Welding Machine",
                 "Pump", "Motor", "Furnace / Oven", "Conveyor", "Lighting", "Other"]

# Words used to fill the Trie (autocomplete)
APPLIANCE_NAMES = [
    "Air Conditioner", "Air Cooler", "Air Purifier", "Air Fryer", "LED Bulb", "CFL Bulb",
    "Tube Light", "Ceiling Fan", "Table Fan", "Exhaust Fan", "Refrigerator", "Television",
    "Washing Machine", "Water Heater", "Geyser", "Microwave Oven", "Induction Cooktop",
    "Electric Kettle", "Mixer Grinder", "Desktop Computer", "Laptop Charger", "Wi-Fi Router",
    "Water Pump", "Iron Box", "Vacuum Cleaner", "Dishwasher", "Toaster", "Hair Dryer",
]
MACHINE_NAMES = [
    "CNC Machine", "Lathe Machine", "Milling Machine", "Drilling Machine", "Grinding Machine",
    "Air Compressor", "Compressor", "Industrial Fan", "Welding Machine", "Water Pump",
    "Hydraulic Press", "Conveyor Belt", "Induction Furnace", "Packaging Machine",
    "Cooling Chiller", "Industrial Oven", "Power Saw", "Boiler Feed Pump",
]

# Which "room" or "area" each category belongs to (used by the NetworkX graph)
HOUSE_ROOMS = {
    "Television": "Living Room", "Air Conditioner": "Living Room", "Lighting": "Living Room",
    "Refrigerator": "Kitchen", "Microwave": "Kitchen",
    "Fan": "Bedroom", "Computer": "Bedroom",
    "Washing Machine": "Utility Area", "Water Heater": "Utility Area",
    "Water Pump": "Utility Area", "Iron": "Utility Area",
}
FACTORY_AREAS = {
    "Production": ("cnc", "lathe", "weld", "mill", "drill", "grind", "press", "machine"),
    "Utility": ("compressor", "pump", "motor", "furnace", "boiler", "chiller", "oven"),
}

LEVEL_ORDER = {"warning": 0, "major": 1, "tip": 2, "info": 3}   # sort order for recommendations


# ---------------------------------------------------------------------------
# DATA STRUCTURES: Stack and Trie
# ---------------------------------------------------------------------------
class Stack:
    """Simple LIFO stack (last in, first out) built on a Python list."""

    def __init__(self, max_size=None):
        self._items = []
        self.max_size = max_size

    def push(self, item):
        self._items.append(item)
        if self.max_size and len(self._items) > self.max_size:
            self._items.pop(0)                        # forget the oldest entry

    def pop(self):
        return self._items.pop() if self._items else None

    def peek(self):
        return self._items[-1] if self._items else None

    def is_empty(self):
        return len(self._items) == 0

    def size(self):
        return len(self._items)

    def items(self):
        """Latest first: latest change, previous change, older change..."""
        return list(reversed(self._items))


class TrieNode:
    def __init__(self):
        self.children = {}        # dictionary: letter -> TrieNode
        self.word = None          # the full word if a word ends at this node


class Trie:
    """Prefix tree used for appliance/machine name autocomplete."""

    def __init__(self, words=()):
        self.root = TrieNode()
        for word in words:
            self.insert(word)

    def insert(self, word):
        node = self.root
        for letter in word.lower():
            node = node.children.setdefault(letter, TrieNode())
        node.word = word

    def suggest(self, prefix, limit=8):
        """Return up to `limit` words that start with `prefix` (case-insensitive)."""
        node = self.root
        for letter in prefix.lower().strip():
            if letter not in node.children:
                return []
            node = node.children[letter]
        found = []
        self._collect(node, found)
        return sorted(found)[:limit]

    def _collect(self, node, found):
        """RECURSION: visit this node, then every child node, gathering finished words."""
        if node.word:
            found.append(node.word)
        for child in node.children.values():
            self._collect(child, found)


HOUSE_TRIE = Trie(APPLIANCE_NAMES)
FACTORY_TRIE = Trie(MACHINE_NAMES)


# ---------------------------------------------------------------------------
# OOP: devices (inheritance)
# ---------------------------------------------------------------------------
class ElectricalDevice:
    """Base class with the properties and maths shared by appliances and machines."""
    kind = "Device"

    def __init__(self, name, wattage, quantity, hours_per_day, days_per_month,
                 category="Other", device_id=None):
        self.device_id = device_id
        self.name = name
        self.category = category
        self.wattage = float(wattage)
        self.quantity = int(quantity)
        self.hours_per_day = float(hours_per_day)
        self.days_per_month = int(days_per_month)

    def connected_load_w(self):
        return self.wattage * self.quantity

    def monthly_kwh(self):
        """monthly_kwh = (wattage x quantity x hours/day x days/month) / 1000"""
        return (self.wattage * self.quantity * self.hours_per_day * self.days_per_month) / 1000

    def label(self):
        """Short unique label for charts, e.g. 'LED Bulb 9W x6' (so 9W and 12W bulbs stay separate)."""
        return f"{self.name} {self.wattage:g}W x{self.quantity}"

    def describe(self):
        return f"{self.name}: {self.wattage:g} W x {self.quantity}"

    def to_dict(self):
        return {
            "id": self.device_id, "name": self.name, "category": self.category,
            "kind": self.kind, "label": self.label(), "wattage": self.wattage,
            "quantity": self.quantity, "hours_per_day": self.hours_per_day,
            "days_per_month": self.days_per_month,
            "connected_kw": round(self.connected_load_w() / 1000, 3),
        }


class Appliance(ElectricalDevice):
    """A household appliance."""
    kind = "Appliance"

    def describe(self):                                # polymorphism: overrides the base method
        return f"{super().describe()} for {self.hours_per_day:g} h/day"


class Machine(ElectricalDevice):
    """A factory machine; usually runs on working days only."""
    kind = "Machine"

    def describe(self):
        return f"{super().describe()} for {self.hours_per_day:g} h/day, {self.days_per_month} working days"


def make_device(row, consumer_type):
    """Build the right class from a database row (dict)."""
    device_class = Machine if consumer_type == "factory" else Appliance
    return device_class(row["name"], row["wattage"], row["quantity"], row["hours_per_day"],
                        row["days_per_month"], row["category"], row["id"])


# ---------------------------------------------------------------------------
# BILLING
# ---------------------------------------------------------------------------
def slab_segments(units, tariff=DEFAULT_TARIFF):
    """
    GENERATOR (yield): produces one tuple per slab that the consumption reaches:
        (slab_label, units_in_slab, rate, amount)
    Telescopic billing: 180 units = 50@3.35 + 50@4.25 + 50@5.35 + 30@7.20
    """
    remaining = units
    previous_max = 0
    for slab in tariff:
        if remaining <= 0:
            break
        if slab["max"] is None:
            width = remaining                                  # last slab has no upper limit
            label = f"Above {previous_max}"
        else:
            width = slab["max"] - previous_max                 # 50, 50, 50, 50, 50 ...
            label = f"{slab['min']} - {slab['max']}"
        used = min(remaining, width)
        yield (label, used, slab["rate"], used * slab["rate"])
        remaining -= used
        if slab["max"] is not None:
            previous_max = slab["max"]


def calculate_bill(units, tariff_mode="default", custom_rate=None, months=1):
    """Approximate bill for `units` kWh consumed in one billing cycle."""
    units = max(float(units), 0.0)
    breakdown = []
    if tariff_mode == "custom" and custom_rate:
        total = units * float(custom_rate)                     # bill = total_kwh x custom_rate
        breakdown.append({"label": "All units", "units": round(units, 2),
                          "rate": float(custom_rate), "amount": round(total, 2)})
        rate_info = f"Custom rate: Rs. {float(custom_rate):.2f} per unit"
    else:
        total = 0.0
        for label, used, rate, amount in slab_segments(units):  # tuple unpacking
            total += amount
            breakdown.append({"label": label, "units": round(used, 2),
                              "rate": rate, "amount": round(amount, 2)})
        rate_info = "Default slab tariff (telescopic): each slab is charged at its own rate"
        tariff_mode = "default"
    total = round(total, 2)
    return {
        "mode": tariff_mode, "total": total, "monthly_average": round(total / months, 2),
        "breakdown": breakdown, "rate_info": rate_info,
        "effective_rate": round(total / units, 2) if units else 0.0,
    }


# ---------------------------------------------------------------------------
# STATISTICS (NumPy + SciPy)
# ---------------------------------------------------------------------------
def simulate_daily_profile(avg_daily_kwh, days=30):
    """
    Random numbers: builds a SIMULATED 30-day pattern around the estimated daily average
    (small random variation, sometimes one high-usage day). The seed comes from the data,
    so the same inputs always give the same pattern. SciPy's z-score then finds unusual days.
    """
    if avg_daily_kwh <= 0:
        return {"daily": [], "anomalies": []}
    rng = np.random.default_rng(int(round(avg_daily_kwh * 100)))
    daily = rng.normal(avg_daily_kwh, avg_daily_kwh * 0.07, days)
    if rng.random() < 0.5:                                      # sometimes add one heavy day
        daily[int(rng.integers(0, days))] *= rng.uniform(1.5, 1.9)
    daily = np.clip(daily, 0, None)
    z_scores = stats.zscore(daily) if daily.std() > 0 else np.zeros(days)
    anomalies = [{"day": int(i) + 1, "kwh": round(float(daily[i]), 2), "z": round(float(z_scores[i]), 2)}
                 for i in np.where(z_scores > 2.5)[0]]
    return {"daily": [round(float(v), 2) for v in daily], "anomalies": anomalies}


def consumption_trend(values):
    """Fit a straight line (np.polyfit) through past report totals and describe the direction."""
    if len(values) < 2:
        return {"direction": "not enough data", "slope": 0.0}
    slope = float(np.polyfit(np.arange(len(values)), np.array(values, dtype=float), 1)[0])
    if abs(slope) < 1:
        direction = "stable"
    else:
        direction = "increasing" if slope > 0 else "decreasing"
    return {"direction": direction, "slope": round(slope, 2)}


# ---------------------------------------------------------------------------
# NETWORKX: device hierarchy (House -> Room -> Device)
# ---------------------------------------------------------------------------
def room_for(device, consumer_type):
    if consumer_type == "factory":
        text = f"{device.category} {device.name}".lower()
        for area, keywords in FACTORY_AREAS.items():
            if any(word in text for word in keywords):         # built-in any()
                return area
        return "Support"
    return HOUSE_ROOMS.get(device.category, "Other Areas")


def build_hierarchy_graph(devices, consumer_type):
    """Directed graph: root -> area/room -> device. Device nodes carry their kWh."""
    graph = nx.DiGraph()
    root = "Factory" if consumer_type == "factory" else "House"
    graph.add_node(root, label=root)
    for device in devices:
        area = room_for(device, consumer_type)
        graph.add_edge(root, area)
        graph.nodes[area]["label"] = area
        node_id = f"device-{device.device_id}"
        graph.add_node(node_id, label=device.name, kwh=device.monthly_kwh())
        graph.add_edge(area, node_id)
    return graph, root


def node_total_kwh(graph, node):
    """RECURSION: a device's kWh is its own; any other node is the sum of its children."""
    children = list(graph.successors(node))
    if not children:
        return graph.nodes[node].get("kwh", 0.0)
    return sum(node_total_kwh(graph, child) for child in children)


def graph_to_tree(graph, node):
    """RECURSION: convert the graph into nested dictionaries for the website."""
    return {
        "name": graph.nodes[node].get("label", node),
        "kwh": round(node_total_kwh(graph, node), 2),
        "children": [graph_to_tree(graph, child) for child in graph.successors(node)],
    }


# ---------------------------------------------------------------------------
# RECOMMENDATIONS (rule based - no machine learning)
# ---------------------------------------------------------------------------
class RecommendationEngine:
    """Looks at the analysis of ONE user and produces safe, informational suggestions."""
    MAJOR_SHARE = 30         # % of total -> "major consumer"
    HIGH_SHARE = 50          # % of total -> "high consumption" warning
    AC_PATTERN = re.compile(r"\b(ac|a/c|air\s*condition\w*)\b", re.IGNORECASE)   # regex
    BULB_PATTERN = re.compile(r"bulb|lamp|light|tube", re.IGNORECASE)

    def __init__(self, consumer_type):
        self.consumer_type = consumer_type

    def generate(self, analysis):
        recs, seen = [], set()                      # a set stops duplicate messages

        def add(level, text):
            if text not in seen:
                seen.add(text)
                recs.append({"level": level, "text": text})

        for d in analysis["devices"]:
            name, share = d["name"], d["percentage"]
            text = f"{d['category']} {d['name']}"
            watts_each, hours = d["wattage"], d["hours_per_day"]

            if share > self.HIGH_SHARE:
                add("warning", f"High consumption: {name} contributes about {share:.0f}% of your estimated consumption.")
            elif share > self.MAJOR_SHARE:
                add("major", f"{name} is a major consumer ({share:.0f}% of your estimated consumption).")

            if self.consumer_type == "house":
                if self.AC_PATTERN.search(text) and hours > 5:
                    per_hour = d["wattage"] * d["quantity"] * d["days_per_month"] / 1000
                    add("tip", f"{name} runs {hours:g} hours/day. Reviewing the usage duration could help - "
                               f"each hour less saves about {per_hour:.1f} kWh per month.")
                if (d["category"] == "Lighting" or self.BULB_PATTERN.search(name)) and watts_each > 20:
                    add("tip", f"{name} is rated {watts_each:g} W each. Consider a lower-wattage LED option "
                               f"with similar brightness.")
                if d["category"] == "Water Heater" and hours > 2:
                    add("tip", f"{name} runs {hours:g} hours/day. Heating water only for the time you need can reduce its share.")
            else:
                if share > self.MAJOR_SHARE or d["monthly_kwh"] > 1500:
                    add("tip", f"{name} uses {d['monthly_kwh']:.0f} kWh/month. Review its operating hours, idle time and scheduling.")
                if hours > 16:
                    add("tip", f"{name} runs {hours:g} hours/day. Check how much of that time is idle running.")

        if self.consumer_type == "factory" and analysis["monthly_kwh"] > 5000:
            add("tip", "Total factory consumption is high. Review operating hours, idle time and production scheduling.")
        if not recs:
            add("info", "No unusual usage patterns found in the data you entered.")
        add("info", "These suggestions are informational. For any electrical work, always consult a qualified electrician.")
        return sorted(recs, key=lambda r: LEVEL_ORDER[r["level"]])


# ---------------------------------------------------------------------------
# MAIN ANALYZER (association: user + devices + recommendation engine)
# ---------------------------------------------------------------------------
class ElectricityAnalyzer:
    def __init__(self, user, device_rows):
        self.user = user                                                       # association 1: the user
        self.consumer_type = user.get("consumer_type") or "house"
        self.months = 2 if user.get("billing_cycle") == 2 else 1
        self.devices = [make_device(row, self.consumer_type) for row in device_rows]   # association 2
        self.recommender = RecommendationEngine(self.consumer_type)             # association 3

    def _bill(self, cycle_kwh):
        return calculate_bill(cycle_kwh, self.user.get("tariff_mode"), self.user.get("custom_rate"), self.months)

    def analyze(self):
        """Run every calculation and return one dictionary the website, PDF and email all share."""
        # map() + lambda: collect each device's monthly kWh into a NumPy array
        kwh = np.array(list(map(lambda dev: dev.monthly_kwh(), self.devices)), dtype=float)
        monthly_total = float(kwh.sum()) if kwh.size else 0.0
        percentages = kwh / monthly_total * 100 if monthly_total > 0 else np.zeros(kwh.size)
        z_scores = stats.zscore(kwh) if kwh.size >= 4 and kwh.std() > 0 else np.zeros(kwh.size)

        rows = []
        for device, value, share, z in zip(self.devices, kwh, percentages, z_scores):
            info = device.to_dict()
            info.update(monthly_kwh=round(float(value), 2), cycle_kwh=round(float(value) * self.months, 2),
                        percentage=round(float(share), 1), z_score=round(float(z), 2))
            rows.append(info)
        # lambda used as the sort key: biggest consumer first
        rows = sorted(rows, key=lambda d: d["monthly_kwh"], reverse=True)

        cycle_kwh = round(monthly_total * self.months, 2)
        avg_daily = monthly_total / 30                    # same as cycle_kwh / (30 x months)
        connected_kw = sum(map(lambda dev: dev.connected_load_w(), self.devices)) / 1000
        highest = max(rows, key=lambda d: d["monthly_kwh"]) if rows else None
        lowest = min(rows, key=lambda d: d["monthly_kwh"]) if rows else None

        highest_message = None
        if highest and monthly_total > 0 and highest["percentage"] >= self.recommender.MAJOR_SHARE:
            highest_message = (f"Your {highest['name']} contributes approximately "
                               f"{highest['percentage']:.0f}% of your estimated consumption.")

        profile = simulate_daily_profile(avg_daily)
        outliers = [{"name": d["name"], "z": d["z_score"]} for d in rows if d["z_score"] >= 1.5]
        graph, root = build_hierarchy_graph(self.devices, self.consumer_type)

        analysis = {
            "consumer_type": self.consumer_type, "billing_cycle": self.months, "months": self.months,
            "devices": rows, "device_count": len(rows),
            "unit_count": sum(d["quantity"] for d in rows),
            "monthly_kwh": round(monthly_total, 2), "cycle_kwh": cycle_kwh,
            "monthly_average_kwh": round(cycle_kwh / self.months, 2),
            "avg_daily_kwh": round(avg_daily, 2), "connected_load_kw": round(connected_kw, 2),
            "highest": highest, "lowest": lowest, "highest_message": highest_message,
            "bill": self._bill(cycle_kwh),
            "statistics": {
                "mean_kwh": round(float(kwh.mean()), 2) if kwh.size else 0.0,
                "std_kwh": round(float(kwh.std()), 2) if kwh.size else 0.0,
                "median_kwh": round(float(np.median(kwh)), 2) if kwh.size else 0.0,
                "device_outliers": outliers,
                "daily_profile": profile["daily"], "anomaly_days": profile["anomalies"],
            },
            "hierarchy": graph_to_tree(graph, root) if self.devices else None,
        }
        analysis["recommendations"] = self.recommender.generate(analysis)
        return analysis

    def what_if(self, device_id, new_hours):
        """Temporary calculation - nothing is saved. Compares current usage with new_hours/day."""
        target = next((d for d in self.devices if d.device_id == device_id), None)
        if target is None:
            raise ValueError("Device not found.")
        current_monthly = sum(d.monthly_kwh() for d in self.devices)
        old_device_kwh = target.monthly_kwh()
        new_device_kwh = target.wattage * target.quantity * new_hours * target.days_per_month / 1000
        new_monthly = current_monthly - old_device_kwh + new_device_kwh
        current_bill = self._bill(current_monthly * self.months)
        new_bill = self._bill(new_monthly * self.months)
        return {
            "device_name": target.name, "months": self.months,
            "current_hours": target.hours_per_day, "new_hours": new_hours,
            "current_device_kwh": round(old_device_kwh, 2), "new_device_kwh": round(new_device_kwh, 2),
            "saving_kwh_monthly": round(old_device_kwh - new_device_kwh, 2),
            "current_cycle_kwh": round(current_monthly * self.months, 2),
            "new_cycle_kwh": round(new_monthly * self.months, 2),
            "current_bill": current_bill["total"], "new_bill": new_bill["total"],
            "bill_saving": round(current_bill["total"] - new_bill["total"], 2),
        }


# ---------------------------------------------------------------------------
# SAMPLE DATA (random numbers)
# ---------------------------------------------------------------------------
def _hours(low, high):
    """Random hours rounded to the nearest half hour."""
    return round(random.uniform(low, high) * 2) / 2


def generate_sample_devices(consumer_type):
    """Return a list of sensible demo devices with random (but realistic) usage."""
    if consumer_type == "factory":
        plan = [("CNC Machine", "CNC Machine", 5000, 3, (8, 11), (24, 26)),
                ("Compressor", "Compressor", 7500, 1, (6, 9), (24, 26)),
                ("Industrial Fan", "Industrial Fan", 500, 10, (8, 11), (24, 26)),
                ("Welding Machine", "Welding Machine", 4000, 4, (3, 6), (18, 22)),
                ("Pump", "Pump", 3000, 2, (4, 8), (24, 26))]
    else:
        plan = [("LED Bulb", "Lighting", 9, 6, (4, 7), (30, 30)),
                ("Ceiling Fan", "Fan", 75, 4, (6, 10), (30, 30)),
                ("Air Conditioner", "Air Conditioner", 1500, 2, (4, 7), (25, 30)),
                ("Refrigerator", "Refrigerator", 200, 1, (11, 13), (30, 30)),
                ("TV", "Television", 120, 1, (3, 6), (30, 30)),
                ("Computer", "Computer", 100, 1, (3, 8), (26, 30)),
                ("Water Heater", "Water Heater", 2000, 1, (0.5, 1.5), (30, 30))]
    return [{"name": n, "category": c, "wattage": w, "quantity": q,
             "hours_per_day": _hours(*h), "days_per_month": random.randint(*d)}
            for n, c, w, q, h, d in plan]
