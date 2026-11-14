"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.members"
      endpoints={["GET/POST /organizations/{org}/members", "PATCH/DELETE /organizations/{org}/members/{id}（If-Match）"]}
    />
  );
}
