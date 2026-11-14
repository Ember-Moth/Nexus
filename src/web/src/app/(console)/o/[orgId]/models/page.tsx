"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.models"
      endpoints={["GET /models?organization_id={organization_id}&project_id=&key_id=", "GET /organizations/{organization_id}/pricing?model_id="]}
    />
  );
}
