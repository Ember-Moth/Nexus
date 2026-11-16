-- One schema per module plus gateway_v1, the only schema Relay nodes may call into. All of them
-- belong to nexus_migrator; the module roles (nexus_identity, ...) and nexus_bridge, which owns
-- the gateway functions, only use them. Roles and database grants come from provisioning
-- (nexus provision-database), not from migrations.

CREATE SCHEMA identity;
CREATE SCHEMA catalog;
CREATE SCHEMA billing;
CREATE SCHEMA publication;
CREATE SCHEMA reporting;
CREATE SCHEMA gateway_v1;

GRANT USAGE ON SCHEMA billing TO nexus_billing;
GRANT USAGE ON SCHEMA billing TO nexus_bridge;
GRANT USAGE ON SCHEMA catalog TO nexus_catalog;
GRANT USAGE ON SCHEMA catalog TO nexus_bridge;
GRANT USAGE ON SCHEMA gateway_v1 TO nexus_gateway;
GRANT USAGE ON SCHEMA gateway_v1 TO nexus_bridge;
GRANT USAGE ON SCHEMA identity TO nexus_identity;
GRANT USAGE ON SCHEMA identity TO nexus_bridge;
GRANT USAGE ON SCHEMA publication TO nexus_publication;
GRANT USAGE ON SCHEMA publication TO nexus_bridge;
GRANT USAGE ON SCHEMA reporting TO nexus_reporting;

-- Functions are private by default: a function is callable only by the roles granted below.
ALTER DEFAULT PRIVILEGES FOR ROLE nexus_migrator REVOKE ALL ON FUNCTIONS FROM PUBLIC;

-- The owning roles create their functions and take over their tables during this migration;
-- 16_ownership.sql revokes CREATE again.
GRANT CREATE ON SCHEMA identity TO nexus_identity;
GRANT CREATE ON SCHEMA catalog TO nexus_catalog;
GRANT CREATE ON SCHEMA billing TO nexus_billing;
GRANT CREATE ON SCHEMA publication TO nexus_publication;
GRANT CREATE ON SCHEMA gateway_v1 TO nexus_bridge;
