ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check;
ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check CHECK(jsonb_typeof(tool_dimensions)='array'
 AND tool_dimensions <@ '["web_search_requests","file_search_calls","vector_storage_byte_seconds","code_execution_seconds"]');
ALTER TABLE billing.request_runs ADD COLUMN execution_mode text NOT NULL DEFAULT 'none'
 CHECK(execution_mode IN ('none','bundled','metered','preloaded')), ADD COLUMN execution_seconds_limit integer NOT NULL DEFAULT 0
 CHECK(execution_seconds_limit BETWEEN 0 AND 1000000000);
CREATE TRIGGER immutable_execution_bounds BEFORE UPDATE OF execution_mode,execution_seconds_limit ON billing.request_runs
 FOR EACH ROW WHEN ((OLD.execution_mode,OLD.execution_seconds_limit) IS DISTINCT FROM (NEW.execution_mode,NEW.execution_seconds_limit))
 EXECUTE FUNCTION billing.protect_search_limit();
ALTER TABLE billing.request_attempts ADD COLUMN meter_supplier_id uuid;
CREATE TABLE billing.execution_usage_receipts(
 request_id uuid PRIMARY KEY REFERENCES billing.request_runs, deployment_id uuid NOT NULL,supplier_id uuid NOT NULL,
 reference_digest varchar(64) NOT NULL,confirmed_seconds integer NOT NULL CHECK(confirmed_seconds>=0),
 UNIQUE(supplier_id,reference_digest)
);
GRANT SELECT,INSERT ON billing.execution_usage_receipts TO nexus_billing;
