CREATE OR REPLACE FUNCTION catalog.v1_eligible(model uuid, dep uuid, snapshot jsonb, org uuid, inputs bigint, outputs bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE m catalog.models; candidate jsonb; resource jsonb;
BEGIN
 PERFORM revision FROM catalog.source_revision WHERE id=1 FOR SHARE;
 SELECT * INTO m FROM catalog.models WHERE id=model AND status='active';
 SELECT v INTO resource FROM jsonb_array_elements(snapshot->'resources'->'models') v WHERE v->>'id'=model::text;
 IF m.id IS NULL OR resource IS NULL OR (resource->>'version')::int<>m.version THEN RAISE EXCEPTION 'model_unavailable'; END IF;
 IF inputs IS NULL OR outputs IS NULL OR inputs NOT BETWEEN 0 AND m.max_input_tokens OR outputs NOT BETWEEN 0 AND m.max_output_tokens
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 FOR candidate IN SELECT d FROM jsonb_array_elements(snapshot->'resources'->'deployments') d
 WHERE d->>'model_id'=model::text AND (dep IS NULL OR d->>'id'=dep::text) AND EXISTS(
 SELECT FROM jsonb_array_elements(snapshot->'resources'->'routes') route,
 jsonb_array_elements(route->'candidates') c WHERE route->>'organization_id'=org::text
 AND route->>'model_id'=model::text AND c->>'deployment_id'=d->>'id') LOOP
  -- A fallback candidate must fit the reservation frozen for this request.
  IF candidate ? 'relay_profile' AND NOT (
    inputs >= (candidate #>> '{relay_profile,input_ceiling}')::bigint
    AND outputs BETWEEN 1 AND (candidate #>> '{relay_profile,output_ceiling}')::bigint
  ) THEN CONTINUE; END IF;
  IF EXISTS(SELECT FROM catalog.deployments d JOIN catalog.upstreams u ON u.id=d.upstream_id
    JOIN catalog.upstream_credentials c ON c.id=d.credential_id WHERE d.id=(candidate->>'id')::uuid
    AND d.model_id=model AND d.status='active' AND u.status='active' AND c.status='active'
    AND d.version=(candidate->>'version')::int AND u.version=(candidate->>'upstream_version')::int
    AND c.id=(candidate->>'credential_id')::uuid AND c.version=(candidate->>'credential_version')::int
    AND m.protocols ? u.protocol_family) THEN RETURN m.token_dimensions; END IF;
 END LOOP;
 RAISE EXCEPTION 'deployment_unavailable';
END $$;
