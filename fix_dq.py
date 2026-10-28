import json
D = "grafana/dashboards/marketdata.json"
d = json.load(open(D))
for p in d.get("panels", []):
    if "DQ" in p.get("title", ""):
        for t in p.get("targets", []):
            t["rawSql"] = "SELECT value FROM market_data_aux.dq_metrics WHERE metric = 'gap_vs_calendar' ORDER BY run_ts DESC LIMIT 1"
        print("fixed DQ panel")
json.dump(d, open(D, "w"), indent=2, ensure_ascii=False)
