CREATE FUNCTION gateway_v1.resource_usage(kind text,kid uuid,rev bigint,keys uuid[],key_cap bigint,org_cap bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 IF cardinality(keys)>100000 THEN RAISE EXCEPTION 'invalid_resource_kind'; END IF;
 RETURN publication.v1_resource_usage(kind,kid,keys,key_cap,org_cap);
END $$;
