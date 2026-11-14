"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.members"
      endpoints={["GET/POST /organizations/{organization_id}/members", "PATCH/DELETE /organizations/{organization_id}/members/{id}（If-Match）"]}
    />
  );
}
