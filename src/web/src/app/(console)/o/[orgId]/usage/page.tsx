"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.usage"
      endpoints={["GET /organizations/{org}/usage?group_by=organization|project|key|model|day", "GET /organizations/{org}/usage?include_cost=true（平台财务）"]}
    />
  );
}
