-- Check a real routed, current deployment before acquiring the wallet hold.
CREATE FUNCTION catalog.v1_search_eligible(model uuid,snapshot jsonb,org uuid,inputs bigint,outputs bigint,searches integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
