"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.usage"
      endpoints={["GET /organizations/{organization_id}/usage?group_by=organization|project|key|model|day", "GET /organizations/{organization_id}/usage?include_cost=true（平台财务）"]}
    />
  );
}
