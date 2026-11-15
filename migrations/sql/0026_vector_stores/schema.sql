ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check;
ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check CHECK(
 jsonb_typeof(tool_dimensions)='array' AND tool_dimensions <@ '["web_search_requests","file_search_calls","vector_storage_byte_seconds"]');
ALTER TABLE billing.request_runs ADD COLUMN file_search_limit integer NOT NULL DEFAULT 0 CHECK(file_search_limit BETWEEN 0 AND 1000000000),
 ADD COLUMN storage_byte_seconds numeric(24,0) NOT NULL DEFAULT 0 CHECK(storage_byte_seconds>=0);
CREATE TRIGGER immutable_vector_bounds BEFORE UPDATE OF file_search_limit,storage_byte_seconds ON billing.request_runs
 FOR EACH ROW WHEN ((OLD.file_search_limit,OLD.storage_byte_seconds) IS DISTINCT FROM (NEW.file_search_limit,NEW.storage_byte_seconds))
 EXECUTE FUNCTION billing.protect_search_limit();
