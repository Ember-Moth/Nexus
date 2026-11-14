CREATE OR REPLACE FUNCTION gateway_v1.start_attempt(rid uuid,aid uuid,seq integer,dep uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; req jsonb; snap jsonb; model uuid; org uuid; expected_format text;
BEGIN
 p=publication.v1_principal(); req=billing.v1_status(rid,p.node_id);
 IF req IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 model=(req->>'model_id')::uuid;org=(req->>'organization_id')::uuid;
 snap=publication.v1_snapshot((req->>'config_revision')::bigint,false)->'manifest';
 IF p.applied_revision<(req->>'config_revision')::bigint OR EXISTS(
  SELECT FROM jsonb_array_elements_text(snap->'required_capabilities') cap WHERE NOT cap=ANY(p.capabilities))
 THEN RAISE EXCEPTION 'configuration_not_applied'; END IF;
 PERFORM identity.v1_authorize((req->>'api_key_id')::uuid,model);
 PERFORM catalog.v1_eligible(model,dep,snap,org,(req->>'input_limit')::bigint,(req->>'output_limit')::bigint);
 SELECT d->>'usage_format' INTO STRICT expected_format
 FROM jsonb_array_elements(snap->'resources'->'deployments') d WHERE d->>'id'=dep::text;
 RETURN billing.v1_attempt(org,rid,aid,dep,model,seq,clock_timestamp(),p.node_id,expected_format);
END $$;
