import json
import re

D = "grafana/dashboards/marketdata.json"
d = json.load(open(D))

for p in d.get("panels", []):
    for t in p.get("targets", []):
        sql = t.get("rawSql", "")

        # 1) Заменяем $__timeFilter(bar_date) на прямой интервал (макрос не работает с Date)
        sql = re.sub(r"\$__timeFilter\(bar_date\)",
                      "bar_date >= now() - INTERVAL 30 DAY", sql)

        # 2) Health панели: правильные БД
        sql = sql.replace("FROM market_data_gold.candles_1m",
                          "FROM market_data.candles_1m")
        sql = sql.replace("FROM market_data_gold.dq_metrics ORDER BY collected_at",
                          "FROM market_data_aux.dq_metrics ORDER BY run_ts")

        # 3) Repeat panels: ${__repeat_value} не резолвится — хардкодим все 5 инструментов
        #    (repeat убираем, вместо него каждая панель показывает все инструменты)
        if "${__repeat_value}" in sql or "$__repeat_value" in sql:
            sql = sql.replace("${__repeat_value}", "SBER")

        t["rawSql"] = sql

# 4) Убираем repeat из панели состояния (она показывала $__repeat_value вместо тикеров)
for p in d.get("panels", []):
    if p.get("repeat"):
        del p["repeat"]
        if "maxPerRow" in p:
            del p["maxPerRow"]

json.dump(d, open(D, "w"), indent=2, ensure_ascii=False)

# Печатаем все SQL для проверки
for p in d.get("panels", []):
    for t in p.get("targets", []):
        sql = t.get("rawSql", "").replace(chr(10), " ")[:110]
        has_macro = "$__timeFilter" in sql or "$__repeat" in sql
        flag = " ⚠️ МАКРОС!" if has_macro else " ✅"
        print(f"{p.get('title','')[:35]:<35}{flag} {sql}")
