-- Tables and views that belong to a module role rather than to nexus_migrator. Everything is created
-- by nexus_migrator first, so foreign keys between owners need no REFERENCES grants.

ALTER TABLE billing.container_extensions OWNER TO nexus_billing;
ALTER TABLE billing.container_operations OWNER TO nexus_billing;
ALTER TABLE billing.container_sessions OWNER TO nexus_billing;
ALTER TABLE billing.vector_storage_contracts OWNER TO nexus_billing;
ALTER TABLE billing.vector_storage_days OWNER TO nexus_billing;
ALTER TABLE billing.vector_storage_segments OWNER TO nexus_billing;

ALTER TABLE publication.batch_items OWNER TO nexus_publication;
ALTER TABLE publication.container_files OWNER TO nexus_publication;
ALTER TABLE publication.container_operations OWNER TO nexus_publication;
ALTER TABLE publication.conversations OWNER TO nexus_publication;
ALTER TABLE publication.execution_containers OWNER TO nexus_publication;
ALTER TABLE publication.inactive_keys OWNER TO nexus_publication;
ALTER TABLE publication.message_batches OWNER TO nexus_publication;
ALTER TABLE publication.purge_request_items OWNER TO nexus_publication;
ALTER TABLE publication.purge_requests OWNER TO nexus_publication;
ALTER TABLE publication.response_jobs OWNER TO nexus_publication;
ALTER TABLE publication.stored_responses OWNER TO nexus_publication;
ALTER TABLE publication.tenant_files OWNER TO nexus_publication;
ALTER TABLE publication.upstream_purges OWNER TO nexus_publication;
ALTER TABLE publication.vector_file_batches OWNER TO nexus_publication;
ALTER TABLE publication.vector_stores OWNER TO nexus_publication;
ALTER VIEW publication.upstream_resources OWNER TO nexus_publication;


REVOKE CREATE ON SCHEMA identity FROM nexus_identity;
REVOKE CREATE ON SCHEMA catalog FROM nexus_catalog;
REVOKE CREATE ON SCHEMA billing FROM nexus_billing;
REVOKE CREATE ON SCHEMA publication FROM nexus_publication;
REVOKE CREATE ON SCHEMA gateway_v1 FROM nexus_bridge;
