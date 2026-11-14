"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.models"
      endpoints={["GET /models?organization_id={org}&project_id=&key_id=", "GET /organizations/{org}/pricing?model_id="]}
    />
  );
}
