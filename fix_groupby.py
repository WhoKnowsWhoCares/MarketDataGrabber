import json
D = "grafana/dashboards/marketdata.json"
d = json.load(open(D))
for p in d.get("panels", []):
    if "Состояние" in p.get("title", ""):
        for t in p.get("targets", []):
            if "instrument_uid AS metric" in t.get("rawSql", "") and "GROUP BY" not in t.get("rawSql", ""):
                t["rawSql"] = t["rawSql"].rstrip() + " GROUP BY instrument_uid"
                print("added GROUP BY")
json.dump(d, open(D, "w"), indent=2, ensure_ascii=False)
print("done")
