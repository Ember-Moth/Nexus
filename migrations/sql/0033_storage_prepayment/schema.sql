-- Prepaid storage coverage per store. See storage.sql.
ALTER TABLE billing.vector_storage_contracts
 ADD COLUMN hold_id uuid REFERENCES billing.request_runs,
 ADD COLUMN prepaid_until timestamptz,
 ADD COLUMN prepaid_bytes bigint NOT NULL DEFAULT 0 CHECK(prepaid_bytes>=0),
 ADD COLUMN renew_after timestamptz NOT NULL DEFAULT clock_timestamp(),
 ADD COLUMN renewal_failed_at timestamptz,
 ADD COLUMN lapsed_at timestamptz,
 ADD COLUMN lapse_action text CHECK(lapse_action IN ('delete','suspend')),
 ADD CHECK((lapsed_at IS NULL)=(lapse_action IS NULL));
CREATE INDEX vector_storage_renewal ON billing.vector_storage_contracts(renew_after) WHERE NOT closed;
