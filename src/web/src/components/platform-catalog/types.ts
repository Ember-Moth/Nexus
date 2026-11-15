import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

// Local aliases of generated contract types used by the platform catalog pages.

export type UserView = Schemas["UserView"];
export type SetupIssued = Schemas["SetupIssued"];
export type PlatformRolesView = Schemas["PlatformRolesView"];
export type AuthPolicyView = Schemas["AuthPolicyView"];

export type CatalogModel = Schemas["ModelView"];
export type ModelCreate = Schemas["ModelCreate"];
export type ModelPatch = Schemas["ModelPatch"];
export type ProtocolFamily = Schemas["ProtocolFamily"];
export type TokenDimension = Schemas["Dimension"];
export type ToolDimension = Schemas["ToolDimension"];
export type CatalogStatus = Schemas["Status"];

export type Upstream = Schemas["UpstreamView"];
export type UpstreamPatch = Schemas["UpstreamPatch"];
export type Credential = Schemas["CredentialView"];

export type CatalogDeployment = Schemas["DeploymentView"];
export type DeploymentCreate = Schemas["DeploymentCreate"];
export type DeploymentPatch = Schemas["DeploymentPatch"];
export type RelayProfile = Schemas["RelayProfile"];

export type PriceVersion = Schemas["PriceView"];
export type PriceRate = Schemas["Rate"];
export type DiscountVersion = Schemas["DiscountView"];

export type PriceKind = "sale" | "cost";
