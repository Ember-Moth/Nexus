-- Check a real routed, current deployment before acquiring the wallet hold.
CREATE FUNCTION catalog.v1_execution_eligible(model uuid,snapshot jsonb,org uuid,inputs bigint,outputs bigint,searches integer,mode text,seconds integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
