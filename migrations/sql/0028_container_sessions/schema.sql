ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check;
ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check CHECK(jsonb_typeof(tool_dimensions)='array' AND
 tool_dimensions <@ '["web_search_requests","file_search_calls","vector_storage_byte_seconds","code_execution_seconds","container_1g_minutes","container_4g_minutes","container_16g_minutes","container_64g_minutes"]');
ALTER TABLE billing.execution_usage_receipts ALTER COLUMN confirmed_seconds TYPE bigint;
ALTER TABLE billing.request_runs ADD COLUMN container_origin uuid, ADD COLUMN container_handle text, ADD COLUMN container_unit text CHECK(container_unit IN ('container_1g_minutes','container_4g_minutes','container_16g_minutes','container_64g_minutes')),
 ADD COLUMN container_minutes_limit integer NOT NULL DEFAULT 0 CHECK(container_minutes_limit BETWEEN 0 AND 1000000000),
 ADD COLUMN container_tariff text CHECK(container_tariff IN ('session_20m','minute_min_5'));
CREATE TRIGGER immutable_container_limits BEFORE UPDATE OF container_unit,container_minutes_limit,container_tariff ON billing.request_runs
 FOR EACH ROW WHEN ((OLD.container_unit,OLD.container_minutes_limit,OLD.container_tariff) IS DISTINCT FROM
 (NEW.container_unit,NEW.container_minutes_limit,NEW.container_tariff)) EXECUTE FUNCTION billing.protect_search_limit();
