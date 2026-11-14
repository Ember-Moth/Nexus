import { redirect } from "next/navigation";

export default async function OrganizationIndex({ params }: PageProps<"/o/[orgId]">) {
  const { orgId } = await params;
  redirect(`/o/${orgId}/overview`);
}
