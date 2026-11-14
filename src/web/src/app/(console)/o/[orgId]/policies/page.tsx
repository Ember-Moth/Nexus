"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.policies"
      endpoints={["GET/POST /organizations/{organization_id}/access-policies", "PATCH /organizations/{organization_id}/access-policies/{id}（If-Match）"]}
    />
  );
}
