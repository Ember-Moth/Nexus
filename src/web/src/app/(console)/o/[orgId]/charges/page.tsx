"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.charges"
      endpoints={["GET /organizations/{organization_id}/charges", "POST /organizations/{organization_id}/exports {kind: \"charges\"}"]}
    />
  );
}
