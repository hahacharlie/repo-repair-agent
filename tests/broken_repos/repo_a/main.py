import yaml
from fetcher import fetch_label
from loader import load_rows

with open("config.yaml") as f:
    config = yaml.safe_load(f)

label = fetch_label(config["label"])
rows = load_rows(config["data_path"])
print(f"{label}: Loaded {len(rows)} rows")
