"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.budgets"
      endpoints={["GET/POST /organizations/{organization_id}/budget-policies", "PATCH /organizations/{organization_id}/budget-policies/{id}（If-Match）", "GET /organizations/{organization_id}/budget-policies/{id}/periods"]}
    />
  );
}
