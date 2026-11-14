"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.upstreams"
      endpoints={["GET/POST /upstreams", "GET/PATCH /upstreams/{id}（If-Match）", "GET/POST /upstreams/{id}/credentials", "POST /upstreams/{id}/credentials/{credential_id}/revoke（If-Match）"]}
    />
  );
}
