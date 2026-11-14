"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.platformAudit"
      endpoints={["GET /audit-events?organization_id=&action="]}
    />
  );
}
