-- Storage is prepaid: each open store keeps one hold covering [now, prepaid_until) at
-- prepaid_bytes. A hold is an ordinary immutable reservation with no attempt. It is replaced
-- (released and reserved again in one transaction) on renewal, growth and daily settlement.
CREATE FUNCTION billing.v1_storage_release(handle uuid,reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c billing.vector_storage_contracts;
BEGIN
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 IF c.hold_id IS NULL OR NOT EXISTS(SELECT FROM billing.request_runs WHERE id=c.hold_id AND reservation_state='held') THEN RETURN false; END IF;
 PERFORM billing.v1_cancel(c.hold_id,c.node_id,reason);
 RETURN true;
END $$;
CREATE FUNCTION billing.v1_storage_hold(handle uuid,until timestamptz,bytes bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE c billing.vector_storage_contracts; hold uuid; dims jsonb; span numeric;
BEGIN
 PERFORM billing.v1_storage_release(handle,'storage_hold_replaced');
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 span=greatest(0,ceil(extract(epoch FROM until-clock_timestamp())));
 IF span>0 AND bytes>0 THEN
  hold=gen_random_uuid();
  SELECT jsonb_agg(k) INTO dims FROM billing.price_versions p CROSS JOIN LATERAL jsonb_object_keys(p.rates) k WHERE p.id=c.sale_id;
  PERFORM billing.v1_reserve_tools(hold,c.organization_id,c.project_id,c.service_account_id,c.api_key_id,c.model_id,
    0,0,86400,encode(sha256(convert_to(hold::text,'UTF8')),'hex'),dims,clock_timestamp(),c.node_id,c.cluster_id,
    c.revision,false,0,0,bytes::numeric*span,c.id);
 END IF;
 UPDATE billing.vector_storage_contracts SET hold_id=hold,prepaid_until=until,prepaid_bytes=greatest(bytes,0) WHERE id=handle;
END $$;
-- Reserve what remains of the prepaid window after a day was drawn from it. If even that
-- cannot be funded, coverage ends now and the lapse policy applies.
CREATE FUNCTION billing.v1_storage_restore(handle uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c billing.vector_storage_contracts;
BEGIN
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 BEGIN
  PERFORM billing.v1_storage_hold(handle,greatest(c.prepaid_until,clock_timestamp()),c.prepaid_bytes);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','invalid_reserve_amount','settlement_backlog') THEN RAISE; END IF;
  UPDATE billing.vector_storage_contracts SET hold_id=NULL,prepaid_until=least(prepaid_until,clock_timestamp()),
   renewal_failed_at=coalesce(renewal_failed_at,clock_timestamp()),renew_after=clock_timestamp() WHERE id=handle;
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
   'vector_storage.renewal_failed',handle,jsonb_build_object('reason',SQLERRM,'prepaid_until',clock_timestamp()));
 END;
END $$;
-- Worker step: renew or grow one store's prepaid hold to `days` ahead, or apply the lapse
-- policy once a failed store's prepaid window has run out.
CREATE FUNCTION billing.v1_renew_storage(days integer,policy text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE c billing.vector_storage_contracts; failure text; horizon interval; now_at timestamptz:=clock_timestamp();
BEGIN
 IF days IS NULL OR days NOT BETWEEN 1 AND 31 OR policy IS NULL OR policy NOT IN ('delete','suspend') THEN
  RAISE EXCEPTION 'invalid_storage_policy'; END IF;
 horizon=make_interval(days=>days);
 SELECT s.* INTO c FROM billing.vector_storage_contracts s WHERE NOT s.closed AND s.renew_after<=clock_timestamp()
  AND s.lapse_action IS DISTINCT FROM 'delete' AND (s.prepaid_until IS NULL OR s.renewal_failed_at IS NOT NULL
   OR s.usage_bytes>s.prepaid_bytes OR s.prepaid_until<clock_timestamp()+horizon*0.75
   OR EXISTS(SELECT FROM billing.request_runs r WHERE r.id=s.hold_id AND r.expires_at<clock_timestamp()+interval '6 hours'))
 ORDER BY s.renew_after,s.id LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN false; END IF;
 BEGIN
  -- Growth headroom keeps a store that is still indexing from replacing its hold every minute.
  PERFORM billing.v1_storage_hold(c.id,clock_timestamp()+horizon,c.usage_bytes+c.usage_bytes/4);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','invalid_reserve_amount','settlement_backlog') THEN RAISE; END IF;
  failure=SQLERRM;
 END;
 IF failure IS NULL THEN
  UPDATE billing.vector_storage_contracts SET renewal_failed_at=NULL,renew_after=clock_timestamp()+interval '1 minute',
   lapsed_at=NULL,lapse_action=NULL WHERE id=c.id;
  IF c.renewal_failed_at IS NOT NULL THEN
   INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
    'vector_storage.renewed',c.id,jsonb_build_object('prepaid_days',days));
  END IF;
  RETURN true;
 END IF;
 IF failure='settlement_backlog' THEN
  UPDATE billing.vector_storage_contracts SET renew_after=clock_timestamp()+interval '1 minute' WHERE id=c.id;
  RETURN true;
 END IF;
 -- The hold funds prepaid_bytes until prepaid_until; at the observed size it lasts proportionally less.
 UPDATE billing.vector_storage_contracts SET renewal_failed_at=coalesce(renewal_failed_at,now_at),
  renew_after=now_at+interval '5 minutes',
  prepaid_until=CASE WHEN prepaid_until IS NULL OR hold_id IS NULL AND usage_bytes>0 THEN now_at
   WHEN usage_bytes>prepaid_bytes THEN least(prepaid_until,now_at+greatest(prepaid_until-now_at,interval '0')
     *(prepaid_bytes::float8/usage_bytes))
   ELSE prepaid_until END WHERE id=c.id RETURNING * INTO c;
 IF c.lapsed_at IS NULL AND c.prepaid_until<=clock_timestamp() THEN
  UPDATE billing.vector_storage_contracts SET lapsed_at=clock_timestamp(),lapse_action=policy WHERE id=c.id;
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
   'vector_storage.lapsed',c.id,jsonb_build_object('reason',failure,'policy',policy,'usage_bytes',c.usage_bytes));
 ELSIF c.renewal_failed_at=now_at THEN
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
   'vector_storage.renewal_failed',c.id,jsonb_build_object('reason',failure,'prepaid_until',c.prepaid_until));
 END IF;
 RETURN true;
END $$;
-- Lapsed stores are not usable by the customer; the delete policy also retires them upstream.
CREATE FUNCTION billing.v1_storage_lapsed(handle uuid) RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT lapse_action FROM billing.vector_storage_contracts WHERE id=handle AND lapsed_at IS NOT NULL AND NOT closed
$$;
CREATE OR REPLACE FUNCTION billing.v1_storage_sample(handle uuid,at_time timestamptz,bytes bigint,expiry timestamptz,closing boolean,uncertain boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE c billing.vector_storage_contracts; start_at timestamptz; end_at timestamptz; until_at timestamptz; quantity numeric; gap boolean; day_id_in uuid;
BEGIN
 IF bytes<0 OR at_time IS NULL OR at_time>clock_timestamp()+interval '5 seconds' THEN RAISE EXCEPTION 'invalid_storage_sample'; END IF;
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 IF c.closed THEN
  IF NOT closing AND NOT uncertain AND at_time>=c.observed_at THEN
   UPDATE billing.vector_storage_contracts SET closed=false,segment=segment+1,observed_at=date_trunc('second',at_time),
      usage_bytes=bytes,expires_at=expiry,observation_unknown=false WHERE id=handle;
  END IF;
  RETURN;
 END IF;
 IF c.observed_at>at_time AND NOT closing THEN RETURN; END IF;
 -- Recording/splitting at whole seconds avoids double rounding at adjacent boundaries.
 at_time=date_trunc('second',greatest(c.observed_at,at_time));
 start_at=c.observed_at; end_at=least(at_time,coalesce(c.expires_at,at_time));
 gap=uncertain OR c.observation_unknown OR at_time-start_at>interval '5 minutes';
 IF closing AND expiry IS NOT NULL THEN end_at=least(end_at,expiry); END IF;
 IF NOT closing AND c.expires_at<at_time AND (expiry IS NULL OR expiry>c.expires_at) THEN
  -- A renewed store bridged a previously observed expiry without a supporting sample.
  gap=true; end_at=at_time;
 END IF;
 IF c.observed_at IS NULL AND uncertain THEN
  INSERT INTO billing.vector_storage_days(store_id,day,segment,byte_seconds,covered_until,state,uncertain,reason)
  VALUES(handle,at_time::date,c.segment,0,at_time,CASE WHEN closing THEN 'ready' ELSE 'open' END,true,
      CASE WHEN closing THEN 'submission_unknown' ELSE 'observation_gap' END) ON CONFLICT(store_id,day,segment) DO NOTHING;
 END IF;
 WHILE start_at<end_at AND (c.usage_bytes>0 OR gap) LOOP
  until_at=least(end_at,date_trunc('day',start_at)+interval '1 day');
  quantity=c.usage_bytes::numeric*extract(epoch FROM until_at-start_at);
  INSERT INTO billing.vector_storage_days(store_id,day,segment,byte_seconds,covered_until,state,uncertain,reason)
  VALUES(handle,start_at::date,c.segment,quantity,until_at,CASE WHEN closing OR until_at::date>start_at::date THEN 'ready' ELSE 'open' END,gap,
     CASE WHEN gap THEN 'observation_gap' END)
  ON CONFLICT(store_id,day,segment) DO UPDATE SET byte_seconds=billing.vector_storage_days.byte_seconds+excluded.byte_seconds,
      covered_until=excluded.covered_until,state=excluded.state,uncertain=billing.vector_storage_days.uncertain OR excluded.uncertain,
      reason=coalesce(billing.vector_storage_days.reason,excluded.reason) WHERE billing.vector_storage_days.state='open'
  RETURNING id INTO day_id_in;
  IF day_id_in IS NULL THEN RAISE EXCEPTION 'storage_period_conflict'; END IF;
  UPDATE billing.vector_storage_segments AS seg SET ended_at=until_at WHERE seg.day_id=day_id_in AND seg.ended_at=start_at
      AND seg.usage_bytes=c.usage_bytes AND seg.uncertain=gap;
  IF NOT FOUND THEN INSERT INTO billing.vector_storage_segments(day_id,started_at,ended_at,usage_bytes,uncertain)
    VALUES(day_id_in,start_at,until_at,c.usage_bytes,gap); END IF;
  start_at=until_at;
 END LOOP;
 UPDATE billing.vector_storage_days SET state='ready' WHERE store_id=handle AND state='open' AND (day<at_time::date OR closing);
 UPDATE billing.vector_storage_contracts SET observation_unknown=uncertain,observed_at=at_time,usage_bytes=bytes,expires_at=expiry,closed=closing WHERE id=handle;
 -- A closed store accrues nothing further; its remaining days settle from the released funds.
 IF closing THEN PERFORM billing.v1_storage_release(handle,'storage_closed'); END IF;
END $$;
CREATE OR REPLACE FUNCTION billing.v1_stage_storage() RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE period billing.vector_storage_days; c billing.vector_storage_contracts; dims jsonb; stamp text; base jsonb; answer jsonb; gap boolean;
 released boolean:=false;
BEGIN
 SELECT * INTO period FROM billing.vector_storage_days WHERE (state='ready' OR (state='review' AND reason IN ('account_frozen','insufficient_funds','budget_exceeded')))
 AND next_attempt<=clock_timestamp() ORDER BY next_attempt,id LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=period.store_id;
 SELECT jsonb_agg(k) INTO dims FROM billing.price_versions p CROSS JOIN LATERAL jsonb_object_keys(p.rates) k WHERE p.id=c.sale_id;
 gap=period.uncertain;
 BEGIN
  -- The prepaid hold already secures this day's storage: release it so the day's own
  -- reservation does not need the same funds twice. A failure below restores it.
  released=billing.v1_storage_release(c.id,'storage_hold_applied');
  PERFORM billing.v1_reserve_tools(period.id,c.organization_id,c.project_id,c.service_account_id,c.api_key_id,c.model_id,
    0,0,86400,encode(sha256(convert_to(period.id::text,'UTF8')),'hex'),dims,period.day::timestamptz,c.node_id,c.cluster_id,
    c.revision,false,0,0,CASE WHEN gap THEN 0 ELSE period.byte_seconds END,c.id);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','price_unavailable','invalid_reserve_amount','currency_config_mismatch','invalid_bounds') THEN RAISE; END IF;
  UPDATE billing.vector_storage_days SET state='review',next_attempt=clock_timestamp()+interval '1 minute',
     reason=SQLERRM WHERE id=period.id;
  RETURN true;
 END;
 INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,sequence,expected_usage_format)
 VALUES(period.attempt_id,period.id,c.deployment_id,c.cost_id,1,'openai.vector_storage.v1');
 stamp=to_char(period.covered_until AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
 base=jsonb_build_object('schema_version',1,'request_id',period.id,'occurred_at',stamp,'config_revision',c.revision::text);
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','attempt.finished','attempt_id',period.attempt_id,
 'payload',jsonb_build_object('outcome',CASE WHEN gap THEN 'unknown' ELSE 'succeeded' END,'elapsed_ms',0,'upstream_request_id',c.id::text,
    'usage_format','openai.vector_storage.v1','raw_usage',CASE WHEN gap THEN NULL ELSE jsonb_build_object('vector_storage_byte_seconds',period.byte_seconds) END)))::text,c.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','request.closed','attempt_id',NULL,
 'payload',jsonb_build_object('delivery_outcome',CASE WHEN gap THEN 'unknown' ELSE 'succeeded' END,'attempt_ids',jsonb_build_array(period.attempt_id),
    'delivered_attempt_id',CASE WHEN gap THEN NULL ELSE period.attempt_id END)))::text,c.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 UPDATE billing.vector_storage_days SET state='staged' WHERE id=period.id;
 IF released THEN PERFORM billing.v1_storage_restore(c.id); END IF;
 RETURN true;
END $$;
CREATE OR REPLACE FUNCTION billing.v1_storage_write(handle uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM billing.v1_storage_writable(c.api_key_id,c.organization_id,c.project_id,(p.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric>0)
 FROM billing.vector_storage_contracts c JOIN billing.price_versions p ON p.id=c.sale_id WHERE c.id=handle;
 -- New bytes would outgrow storage the wallet could not prepay.
 IF EXISTS(SELECT FROM billing.vector_storage_contracts WHERE id=handle AND (renewal_failed_at IS NOT NULL OR lapsed_at IS NOT NULL))
 THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
END $$;
