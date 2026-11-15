CREATE FUNCTION billing.v1_response_attempt(rid uuid,aid uuid,node uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('key',r.api_key_id,'model',r.model_id,'revision',r.config_revision,
 'deployment',a.deployment_id)
 FROM billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id
 WHERE r.id=rid AND a.id=aid AND r.node_id=node AND a.expected_usage_format='openai.responses.v1'
$$;
