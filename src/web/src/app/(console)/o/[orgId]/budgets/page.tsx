"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.budgets"
      endpoints={["GET/POST /organizations/{org}/budget-policies", "PATCH /organizations/{org}/budget-policies/{id}（If-Match）", "GET /organizations/{org}/budget-policies/{id}/periods"]}
    />
  );
}
