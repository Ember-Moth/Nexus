"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.deployments"
      endpoints={["GET/POST /deployments?model_id=&upstream_id=", "GET/PATCH /deployments/{id}（If-Match）"]}
    />
  );
}
