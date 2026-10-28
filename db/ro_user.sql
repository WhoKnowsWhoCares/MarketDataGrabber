-- Read-only пользователь для MCP-сервера. ШАБЛОН: '__MCP_RO_PASSWORD__'
-- подставляется db/apply_ro_user.sh. Вручную и в CI не применять.
CREATE USER IF NOT EXISTS mdg_ro IDENTIFIED BY '__MCP_RO_PASSWORD__';
GRANT SELECT ON market_data.* TO mdg_ro;
GRANT SELECT ON market_data_gold.* TO mdg_ro;
GRANT SELECT ON market_data_aux.* TO mdg_ro;
