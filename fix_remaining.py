import json

D = "grafana/dashboards/marketdata.json"
d = json.load(open(D))

for p in d.get("panels", []):
    title = p.get("title", "")

    # 1) Витрины обновлены: Date → String для stat-панели
    if "Витрины" in title:
        for t in p.get("targets", []):
            t["rawSql"] = "SELECT toString(max(bar_date)) AS bar_date FROM market_data_gold.mart_daily_metrics"

    # 2) Repeat panels: убираем repeat, показываем ВСЕ инструменты (без WHERE по одному)
    if "$__repeat_value" in title or p.get("repeat"):
        p["title"] = title.replace("$__repeat_value", "Состояние инструментов")
        if "repeat" in p:
            del p["repeat"]
        if "maxPerRow" in p:
            del p["maxPerRow"]
        for t in p.get("targets", []):
            sql = t.get("rawSql", "")
            # убираем WHERE instrument_uid = '${instrument}' — показываем все
            import re
            sql = re.sub(r"WHERE instrument_uid\s*=\s*'\$\{instrument\}'", "", sql)
            # metric = uid → показываем все
            sql = sql.replace("'${instrument}' AS metric", "instrument_uid AS metric")
            t["rawSql"] = sql

    # 3) Тренд/Объём/RSI/ATR: заменяем ${instrument} на SBER по умолчанию (переключатель в UI)
    for t in p.get("targets", []):
        sql = t.get("rawSql", "")
        if "${instrument}" in sql:
            sql = sql.replace("${instrument}", "e6123145-9665-43e0-8413-cd61b8aa9b13")
            t["rawSql"] = sql

json.dump(d, open(D, "w"), indent=2, ensure_ascii=False)
print("все панели исправлены")
for p in d.get("panels", []):
    print(f"  {p.get('title','')[:40]:<40} type={p.get('type','')}")
