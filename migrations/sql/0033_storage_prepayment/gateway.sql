CREATE OR REPLACE FUNCTION gateway_v1.vector_binding(handle uuid,kid uuid,rev bigint,cleanup boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE v jsonb;
BEGIN
 v=publication.v1_vector(handle,kid);
 IF v IS NULL OR v->>'state' NOT IN ('active','expired') THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF cleanup THEN PERFORM gateway_v1.preflight(kid,rev,NULL,NULL); ELSE
  PERFORM gateway_v1.preflight(kid,rev,(v->>'model_id')::uuid,(v->>'deployment_id')::uuid); END IF;
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}') d
   WHERE d->>'id'=v->>'deployment_id' AND d->>'credential_id'=v->>'credential_id' AND d->>'credential_version'=v->>'credential_version'
   AND d->>'upstream_version'=v->>'upstream_version' AND d#>>'{relay_profile,endpoint}'='openai_responses')
 THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 -- A store whose prepaid storage lapsed only accepts deletion until the wallet is funded.
 IF NOT cleanup AND billing.v1_storage_lapsed(handle) IS NOT NULL THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 RETURN v||jsonb_build_object('stamp',clock_timestamp());
END $$;
CREATE OR REPLACE FUNCTION gateway_v1.vector_claim() RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT j||jsonb_build_object('stamp',clock_timestamp(),
   'retire',coalesce(billing.v1_storage_lapsed((j->>'id')::uuid)='delete',false)) FROM publication.v1_vector_claim() j
$$;
