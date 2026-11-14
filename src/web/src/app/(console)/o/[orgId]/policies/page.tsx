"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.policies"
      endpoints={["GET/POST /organizations/{org}/access-policies", "PATCH /organizations/{org}/access-policies/{id}（If-Match）"]}
    />
  );
}
