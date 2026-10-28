import json
D = "grafana/dashboards/marketdata.json"
d = json.load(open(D))

fixes = {
    "SELECT now() - max(bar_time) AS age_ms FROM market_data_gold.candles_1m":
        "SELECT now() - max(bar_time) AS age_ms FROM market_data.candles_1m",
    "SELECT gap_vs_calendar FROM market_data_gold.dq_metrics ORDER BY collected_at DESC LIMIT 1":
        "SELECT gap_vs_calendar FROM market_data_aux.dq_metrics ORDER BY run_ts DESC LIMIT 1",
}

changed = 0
for p in d.get("panels", []):
    for t in p.get("targets", []):
        sql = t.get("rawSql", "")
        for old, new in fixes.items():
            if old in sql:
                t["rawSql"] = sql.replace(old, new)
                changed += 1
                print(f"FIXED [{p.get('title','')[:30]}]: {old[:60]} -> {new[:60]}")

json.dump(d, open(D, "w"), indent=2, ensure_ascii=False)
print(f"изменено запросов: {changed}")