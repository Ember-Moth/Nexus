-- catalog functions and trigger functions.
-- Created by their owning role, so their privileges are exactly the grants in 17_privileges.sql.

-- Owned by nexus_catalog.
SET LOCAL ROLE nexus_catalog;

CREATE FUNCTION catalog.v1_credential_current(cid uuid, ver integer) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM revision FROM catalog.source_revision WHERE id=1 FOR SHARE;
 RETURN EXISTS(SELECT FROM catalog.upstream_credentials c JOIN catalog.upstreams u ON u.id=c.upstream_id
 WHERE c.id=cid AND c.version=ver AND c.status='active' AND u.status='active');
END $$;

CREATE FUNCTION catalog.v1_eligible(model uuid, dep uuid, snapshot jsonb, org uuid, inputs bigint, outputs bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE m catalog.models; candidate jsonb; resource jsonb;
BEGIN
 PERFORM revision FROM catalog.source_revision WHERE id=1 FOR SHARE;
 SELECT * INTO m FROM catalog.models WHERE id=model AND status='active';
 SELECT v INTO resource FROM jsonb_array_elements(snapshot->'resources'->'models') v WHERE v->>'id'=model::text;
 IF m.id IS NULL OR resource IS NULL OR (resource->>'version')::int<>m.version THEN RAISE EXCEPTION 'model_unavailable'; END IF;
 IF inputs IS NULL OR outputs IS NULL OR inputs NOT BETWEEN 0 AND m.max_input_tokens::bigint*64 OR outputs NOT BETWEEN 0 AND m.max_output_tokens
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
    AND m.protocols ? u.protocol_family) THEN RETURN m.token_dimensions||m.tool_dimensions; END IF;
 END LOOP;
 RAISE EXCEPTION 'deployment_unavailable';
END $$;

CREATE FUNCTION catalog.v1_execution_eligible(model uuid, snapshot jsonb, org uuid, inputs bigint, outputs bigint, searches integer, mode text, seconds integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE candidate jsonb; dimensions jsonb;
BEGIN
 FOR candidate IN SELECT d FROM jsonb_array_elements(snapshot->'resources'->'deployments') d
 WHERE d->>'model_id'=model::text AND d#>>'{relay_profile,endpoint}' ='anthropic_messages' AND (d#>>'{relay_profile,code_execution}'='true' OR (mode='bundled' AND searches>0))
 AND coalesce((d#>>'{relay_profile,execution_reserve_seconds}')::int,0)>=seconds
 AND d#>>'{relay_profile,tools}'='true' AND coalesce((d#>>'{relay_profile,web_search_ceiling}')::integer,0)>=searches LOOP
  BEGIN
   dimensions=catalog.v1_eligible(model,(candidate->>'id')::uuid,snapshot,org,inputs,outputs);
   IF (mode='bundled' OR dimensions ? 'code_execution_seconds') AND (searches=0 OR dimensions ? 'web_search_requests') THEN RETURN dimensions; END IF;
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM<>'deployment_unavailable' THEN RAISE; END IF;
  END;
 END LOOP;
 RAISE EXCEPTION 'deployment_unavailable';
END $$;

CREATE FUNCTION catalog.v1_file_search_eligible(model uuid, snapshot jsonb, org uuid, inputs bigint, outputs bigint, files integer, searches integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE candidate jsonb; dimensions jsonb;
BEGIN
 FOR candidate IN SELECT d FROM jsonb_array_elements(snapshot->'resources'->'deployments') d
 WHERE d->>'model_id'=model::text AND d#>>'{relay_profile,endpoint}' ='openai_responses'
 AND coalesce((d#>>'{relay_profile,file_search_ceiling}')::integer,0)>=files
 AND d#>>'{relay_profile,tools}'='true' AND coalesce((d#>>'{relay_profile,web_search_ceiling}')::integer,0)>=searches LOOP
  BEGIN
   dimensions=catalog.v1_eligible(model,(candidate->>'id')::uuid,snapshot,org,inputs,outputs);
   IF dimensions ? 'file_search_calls' AND (searches=0 OR dimensions ? 'web_search_requests') THEN RETURN dimensions; END IF;
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM<>'deployment_unavailable' THEN RAISE; END IF;
  END;
 END LOOP;
 RAISE EXCEPTION 'deployment_unavailable';
END $$;

CREATE FUNCTION catalog.v1_model_created(model uuid) RETURNS bigint
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT floor(extract(epoch FROM created_at))::bigint FROM catalog.models WHERE id=model
        $$;

CREATE FUNCTION catalog.v1_search_eligible(model uuid, snapshot jsonb, org uuid, inputs bigint, outputs bigint, searches integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE candidate jsonb; dimensions jsonb;
BEGIN
 FOR candidate IN SELECT d FROM jsonb_array_elements(snapshot->'resources'->'deployments') d
 WHERE d->>'model_id'=model::text AND d#>>'{relay_profile,endpoint}' IN ('openai_responses','anthropic_messages')
 AND d#>>'{relay_profile,tools}'='true' AND coalesce((d#>>'{relay_profile,web_search_ceiling}')::integer,0)>=searches LOOP
  BEGIN
   dimensions=catalog.v1_eligible(model,(candidate->>'id')::uuid,snapshot,org,inputs,outputs);
   IF dimensions ? 'web_search_requests' THEN RETURN dimensions; END IF;
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM<>'deployment_unavailable' THEN RAISE; END IF;
  END;
 END LOOP;
 RAISE EXCEPTION 'deployment_unavailable';
END $$;

RESET ROLE;

-- Owned by nexus_migrator.
CREATE FUNCTION catalog.advance_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
            UPDATE catalog.source_revision SET revision=revision+1 WHERE id=1;
            RETURN NULL; END $$;

CREATE FUNCTION catalog.announce_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
            PERFORM pg_notify('nexus_source_revision','catalog'); RETURN NULL; END $$;
