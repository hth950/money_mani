"""Data exporter for YouTube scraper results."""

import csv
import json
import os
from datetime import datetime


class DataExporter:
    """Exports scraped data to JSON and CSV formats."""

    def __init__(self, output_dir="output"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def to_json(self, data, filename=None):
        """Export data to JSON file."""
        if not filename:
            filename = f"youtube_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        return filepath

    def to_csv(self, data, filename=None):
        """Export data to CSV file.

        Args:
            data: A list of dicts, or a single dict (wrapped in a list).
            filename: Output filename.
        """
        if not filename:
            filename = f"youtube_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = os.path.join(self.output_dir, filename)

        if isinstance(data, dict):
            data = [data]
        if not data:
            return filepath

        flat_data = [self._flatten(item) for item in data]
        all_keys = []
        seen = set()
        for item in flat_data:
            for k in item:
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)

        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(flat_data)

        return filepath

    def export_both(self, data, base_name=None):
        """Export data to both JSON and CSV."""
        if not base_name:
            base_name = f"youtube_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        json_path = self.to_json(data, f"{base_name}.json")
        csv_path = self.to_csv(data, f"{base_name}.csv")
        return {"json": json_path, "csv": csv_path}

    def _flatten(self, obj, prefix=""):
        """Flatten nested dicts for CSV export."""
        items = {}
        if not isinstance(obj, dict):
            return {prefix: obj} if prefix else {}
        for k, v in obj.items():
            key = f"{prefix}_{k}" if prefix else k
            if isinstance(v, dict):
                items.update(self._flatten(v, key))
            elif isinstance(v, list):
                items[key] = "; ".join(str(i) for i in v) if v else ""
            else:
                items[key] = v
        return items
