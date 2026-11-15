-- Reuse the live principal/snapshot and module checks for unmetered discovery
-- and token counting. The node receives only the public names it can serve.
CREATE FUNCTION gateway_v1.preflight(kid uuid, rev bigint, requested_model uuid, dep uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE snap jsonb; k jsonb; org uuid; m jsonb; d jsonb; authorized jsonb;
        visible jsonb := '[]'; limits jsonb;
BEGIN
 snap=publication.v1_snapshot(rev,true)->'manifest';
 SELECT v INTO k FROM jsonb_array_elements(snap->'resources'->'key_verifiers') v
  WHERE v->>'id'=kid::text;
 IF k IS NULL THEN RAISE EXCEPTION 'key_inactive'; END IF;
 org=(k->>'organization_id')::uuid;
 -- A null model checks key liveness even for a key with zero allowed models.
 BEGIN
  PERFORM identity.v1_authorize(kid,NULL);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM <> 'model_forbidden' THEN RAISE; END IF;
 END;
 FOR m IN SELECT v FROM jsonb_array_elements(snap->'resources'->'models') v
   WHERE requested_model IS NULL OR v->>'id'=requested_model::text ORDER BY v->>'public_name' LOOP
  BEGIN
   authorized=identity.v1_authorize(kid,(m->>'id')::uuid);
   IF authorized->>'organization_id' IS DISTINCT FROM k->>'organization_id'
      OR authorized->>'project_id' IS DISTINCT FROM k->>'project_id'
      OR authorized->>'service_account_id' IS DISTINCT FROM k->>'service_account_id'
      OR NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') p
         WHERE p->>'organization_id'=org::text AND p->>'scope_type'='organization' AND p->>'layer'='platform')
      OR EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') p
         WHERE p->>'organization_id'=org::text AND (p->>'scope_type'='organization'
          OR p->>'scope_type'='project' AND p->>'scope_id'=k->>'project_id'
          OR p->>'scope_type'='key' AND p->>'scope_id'=kid::text)
         AND NOT ((p->'allowed_models') ? (m->>'id'))) THEN RAISE EXCEPTION 'model_forbidden'; END IF;
   FOR d IN SELECT v FROM jsonb_array_elements(snap->'resources'->'deployments') v
     WHERE v->>'model_id'=m->>'id' AND v ? 'relay_profile' AND (dep IS NULL OR v->>'id'=dep::text) LOOP
    limits=d->'relay_profile';
    BEGIN
     PERFORM catalog.v1_eligible((m->>'id')::uuid,(d->>'id')::uuid,snap,org,
             (limits->>'input_ceiling')::bigint,(limits->>'output_ceiling')::bigint);
     visible=visible || jsonb_build_array(jsonb_build_object('id',m->>'public_name',
        'object','model','created',catalog.v1_model_created((m->>'id')::uuid),'owned_by','nexus'));
     EXIT;
    EXCEPTION WHEN raise_exception THEN
     IF SQLERRM NOT IN ('deployment_unavailable','model_unavailable') THEN RAISE; END IF;
    END;
   END LOOP;
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM <> 'model_forbidden' OR requested_model IS NOT NULL THEN RAISE; END IF;
  END;
 END LOOP;
 IF requested_model IS NOT NULL AND visible='[]'::jsonb THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 RETURN jsonb_build_object('object','list','data',visible);
END $$;
REVOKE ALL ON FUNCTION gateway_v1.preflight(uuid,bigint,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gateway_v1.preflight(uuid,bigint,uuid,uuid) TO nexus_gateway;
