"use client";

import { SkeletonPage } from "@/components/page";

export default function Page() {
  return (
    <SkeletonPage
      title="nav.items.users"
      endpoints={["GET/POST /users", "POST /users/{id}/password-reset-tokens", "GET/PATCH /users/{id}/platform-roles（If-Match）"]}
    />
  );
}
