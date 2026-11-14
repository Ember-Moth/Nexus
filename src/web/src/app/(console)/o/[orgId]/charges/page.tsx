"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.charges"
      endpoints={["GET /organizations/{org}/charges", "POST /organizations/{org}/exports {kind: \"charges\"}"]}
    />
  );
}
