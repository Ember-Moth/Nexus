-- Downgrade: the unbounded counts of 0034, restored when 0043 is rolled back.
CREATE FUNCTION gateway_v1.resource_usage(kind text,kid uuid,rev bigint,keys uuid[]) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 IF cardinality(keys)>100000 THEN RAISE EXCEPTION 'invalid_resource_kind'; END IF;
 RETURN publication.v1_resource_usage(kind,kid,keys);
END $$;
