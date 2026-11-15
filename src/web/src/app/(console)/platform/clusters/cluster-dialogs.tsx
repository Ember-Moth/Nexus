"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import { useOrganizationOptions } from "@/components/org-picker";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useIdempotentMutation } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import type { GatewayCluster } from "@/lib/api/types";

type ReleaseAccepted = components["schemas"]["ReleaseAccepted"];

/** Capabilities the control plane and relay know about; free-form entries stay possible. */
export const KNOWN_CAPABILITIES = [
  "reservation.v1",
  "usage-events.v1",
  "relay.native.v1",
  "relay.sse.v1",
  "relay.responses-ws.v1",
  "relay.messages-to-responses.v1",
  "relay.responses-to-messages.v1",
  "relay.responses-to-chat.v1",
  "relay.bedrock-converse.v1",
  "relay.bedrock-claude-invoke.v1",
  "relay.bedrock-guardrail-usage.v1",
  "relay.bedrock-request-options.v1",
  "relay.bedrock-settings.v1",
  "relay.prompt-cache.v1",
  "relay.web-search.v1",
  "relay.file-search.v1",
  "relay.claude-execution.v1",
  "relay.openai-containers.v1",
];

const BASE64_KEY = /^[A-Za-z0-9+/]{43}=$/;

function MultiCheck({
  items,
  value,
  onChange,
  label,
}: {
  items: { id: string; label: string }[];
  value: string[];
  onChange: (next: string[]) => void;
  label: string;
}) {
  return (
    <fieldset className="grid gap-1.5">
      <legend className="mb-1.5 text-sm font-medium">{label}</legend>
      <div className="max-h-36 overflow-y-auto rounded-md border p-2">
        {items.map((item) => {
          const checked = value.includes(item.id);
          return (
            <label key={item.id} className="flex items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-muted">
              <Checkbox
                checked={checked}
                aria-label={item.label}
                onCheckedChange={(on) => onChange(on === true ? [...value, item.id] : value.filter((v) => v !== item.id))}
              />
              {item.label}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function useDeploymentOptions(enabled: boolean) {
  return useQuery({
    queryKey: ["catalog", "deployments", "options"],
    queryFn: async () =>
      (await call(() => nexus.GET("/api/v1/deployments", { params: { query: { limit: 100 } } }))).data.items,
    enabled,
  });
}

const clusterSchema = z.object({
  name: z.string().trim().min(1).max(160),
  region: z.string().trim().min(1).max(100),
  organizationIds: z.array(z.string()),
  deploymentIds: z.array(z.string()),
  publicKey: z.string().trim(),
  rotateKey: z.boolean(),
  graceSeconds: z.coerce.number().int().min(0).max(86400),
  active: z.boolean(),
});
type ClusterValues = z.input<typeof clusterSchema>;
type ClusterParsed = z.output<typeof clusterSchema>;

/**
 * Create (POST /gateway-clusters) or edit (PATCH with If-Match). The public key is a
 * Base64-encoded 32-byte Curve25519 key; editing only sends it when rotating.
 */
export function ClusterDialog({
  cluster,
  etag,
  onClose,
}: {
  cluster: GatewayCluster | "new";
  /** ETag from GET /gateway-clusters/{cluster_id}; falls back to the list version. */
  etag?: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const editing = cluster === "new" ? null : cluster;
  const organizations = useOrganizationOptions();
  const deployments = useDeploymentOptions(true);
  const form = useForm<ClusterValues, unknown, ClusterParsed>({
    resolver: zodResolver(clusterSchema),
    defaultValues: {
      name: editing?.name ?? "",
      region: editing?.region ?? "",
      organizationIds: editing?.organization_ids ?? [],
      deploymentIds: editing?.deployment_ids ?? [],
      publicKey: "",
      rotateKey: !editing,
      graceSeconds: 3600,
      active: editing?.active ?? true,
    },
  });
  const rotateKey = useWatch({ control: form.control, name: "rotateKey" });
  const [keyError, setKeyError] = useState(false);

  const save = useMutation({
    mutationFn: async (values: ClusterParsed) => {
      if (!editing) {
        return call(() =>
          nexus.POST("/api/v1/gateway-clusters", {
            body: {
              name: values.name,
              region: values.region,
              organization_ids: values.organizationIds,
              deployment_ids: values.deploymentIds,
              public_key: values.publicKey,
            },
          }),
        );
      }
      return call(() =>
        nexus.PATCH("/api/v1/gateway-clusters/{cluster_id}", {
          params: {
            path: { cluster_id: editing.id },
            header: { "If-Match": etag ?? `"${editing.version}"` },
          },
          body: {
            active: values.active,
            organization_ids: values.organizationIds,
            deployment_ids: values.deploymentIds,
            // Only read by the API when public_key changes.
            key_grace_seconds: values.graceSeconds,
            ...(values.rotateKey ? { public_key: values.publicKey } : {}),
          },
        }),
      );
    },
    onSuccess: () => {
      toast.success(t("clusters.saved"));
      void queryClient.invalidateQueries({ queryKey: ["gateway-clusters"] });
      onClose();
    },
  });

  const submit = form.handleSubmit((values) => {
    const needsKey = !editing || values.rotateKey;
    if (needsKey && !BASE64_KEY.test(values.publicKey)) {
      setKeyError(true);
      return;
    }
    setKeyError(false);
    save.mutate(values);
  });

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-[560px]">
        <form noValidate className="grid gap-4" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{editing ? t("clusters.editTitle", { name: editing.name }) : t("clusters.create")}</DialogTitle>
            <DialogDescription>{t("clusters.formHint")}</DialogDescription>
          </DialogHeader>
          {isConflict(save.error) ? (
            <ConflictAlert
              onReload={() => {
                save.reset();
                void queryClient.invalidateQueries({ queryKey: ["gateway-clusters"] });
                onClose();
              }}
            />
          ) : save.error ? (
            <ErrorAlert error={save.error} />
          ) : null}
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor="cluster-name">{t("clusters.name")}</Label>
              <Input id="cluster-name" disabled={Boolean(editing)} aria-invalid={Boolean(form.formState.errors.name) || undefined} {...form.register("name")} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="cluster-region">{t("clusters.region")}</Label>
              <Input id="cluster-region" disabled={Boolean(editing)} aria-invalid={Boolean(form.formState.errors.region) || undefined} {...form.register("region")} />
            </div>
          </div>
          <Controller
            control={form.control}
            name="organizationIds"
            render={({ field }) => (
              <MultiCheck
                label={t("clusters.organizations")}
                items={(organizations.data ?? []).map((org) => ({ id: org.id, label: org.label }))}
                value={field.value}
                onChange={field.onChange}
              />
            )}
          />
          <Controller
            control={form.control}
            name="deploymentIds"
            render={({ field }) => (
              <MultiCheck
                label={t("clusters.deployments")}
                items={(deployments.data ?? []).map((d) => ({ id: d.id, label: `${d.name} · ${d.region}` }))}
                value={field.value}
                onChange={field.onChange}
              />
            )}
          />
          {editing ? (
            <>
              <Controller
                control={form.control}
                name="active"
                render={({ field }) => (
                  <label className="flex items-center gap-2 text-sm">
                    <Switch checked={field.value} onCheckedChange={field.onChange} />
                    {t("clusters.activeLabel")}
                  </label>
                )}
              />
              <Controller
                control={form.control}
                name="rotateKey"
                render={({ field }) => (
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox checked={field.value} aria-label={t("clusters.rotateKey")} onCheckedChange={(on) => field.onChange(on === true)} />
                    {t("clusters.rotateKey")}
                  </label>
                )}
              />
            </>
          ) : null}
          {rotateKey ? (
            <div className="grid gap-2">
              <Label htmlFor="cluster-key">{t("clusters.publicKey")}</Label>
              <Textarea id="cluster-key" rows={2} className="font-mono text-xs" aria-invalid={keyError || undefined} {...form.register("publicKey")} />
              <span className={keyError ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>{t("clusters.publicKeyHint")}</span>
              {editing ? (
                <div className="grid gap-2">
                  <Label htmlFor="cluster-grace">{t("clusters.graceSeconds")}</Label>
                  <Input id="cluster-grace" inputMode="numeric" className="w-40 tabular-nums" {...form.register("graceSeconds")} />
                </div>
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending || isConflict(save.error)}>
              {save.isPending ? <Spinner /> : null}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const nodeSchema = z.object({
  nodeId: z.string().trim().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i),
  databaseRole: z.string().trim().regex(/^[a-z_][a-z0-9_]{0,62}$/),
  capabilities: z.array(z.string()),
  extra: z.string(),
  active: z.boolean(),
});
type NodeValues = z.infer<typeof nodeSchema>;

export interface NodeDraft {
  node_id: string;
  database_role?: string;
  capabilities?: string[];
  active?: boolean;
}

/** PUT /gateway-clusters/{cluster_id}/principals/{node_id}: registers or updates a node. */
export function NodeDialog({ clusterId, node, onClose }: { clusterId: string; node?: NodeDraft; onClose: () => void }) {
  const t = useT();
  const queryClient = useQueryClient();
  const known = new Set(KNOWN_CAPABILITIES);
  const form = useForm<NodeValues>({
    resolver: zodResolver(nodeSchema),
    defaultValues: {
      nodeId: node?.node_id ?? crypto.randomUUID(),
      databaseRole: node?.database_role ?? "",
      capabilities: (node?.capabilities ?? ["reservation.v1", "usage-events.v1", "relay.native.v1"]).filter((c) => known.has(c)),
      extra: (node?.capabilities ?? []).filter((c) => !known.has(c)).join(", "),
      active: node?.active ?? true,
    },
  });
  const errors = form.formState.errors;
  const save = useMutation({
    mutationFn: async (values: NodeValues) => {
      const extra = values.extra.split(/[\s,]+/).filter(Boolean);
      return call(() =>
        nexus.PUT("/api/v1/gateway-clusters/{cluster_id}/principals/{node_id}", {
          params: { path: { cluster_id: clusterId, node_id: values.nodeId } },
          body: {
            node_id: values.nodeId,
            database_role: values.databaseRole,
            capabilities: [...new Set([...values.capabilities, ...extra])],
            active: values.active,
          },
        }),
      );
    },
    onSuccess: () => {
      toast.success(t("clusters.nodeSaved"));
      void queryClient.invalidateQueries({ queryKey: ["gateway-clusters"] });
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-[560px]">
        <form noValidate className="grid gap-4" onSubmit={form.handleSubmit((values) => save.mutate(values))}>
          <DialogHeader>
            <DialogTitle>{node ? t("clusters.editNode") : t("clusters.registerNode")}</DialogTitle>
            <DialogDescription>{t("clusters.nodeHint")}</DialogDescription>
          </DialogHeader>
          {save.error ? <ErrorAlert error={save.error} /> : null}
          <div className="grid gap-2">
            <Label htmlFor="node-id">{t("clusters.nodeId")}</Label>
            <Input id="node-id" className="font-mono text-xs" disabled={Boolean(node)} aria-invalid={Boolean(errors.nodeId) || undefined} {...form.register("nodeId")} />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="node-role">{t("clusters.databaseRole")}</Label>
            <Input id="node-role" className="font-mono" aria-invalid={Boolean(errors.databaseRole) || undefined} {...form.register("databaseRole")} />
            <span className={errors.databaseRole ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>{t("clusters.databaseRoleHint")}</span>
          </div>
          <Controller
            control={form.control}
            name="capabilities"
            render={({ field }) => (
              <MultiCheck
                label={t("clusters.capabilities")}
                items={KNOWN_CAPABILITIES.map((c) => ({ id: c, label: c }))}
                value={field.value}
                onChange={field.onChange}
              />
            )}
          />
          <div className="grid gap-2">
            <Label htmlFor="node-extra">{t("clusters.extraCapabilities")}</Label>
            <Input id="node-extra" className="font-mono text-xs" {...form.register("extra")} />
          </div>
          <Controller
            control={form.control}
            name="active"
            render={({ field }) => (
              <label className="flex items-center gap-2 text-sm">
                <Switch checked={field.value} onCheckedChange={field.onChange} />
                {t("clusters.activeLabel")}
              </label>
            )}
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? <Spinner /> : null}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * POST /gateway-clusters/{cluster_id}/releases (202, Idempotency-Key). A rollback reuses an
 * older revision's resources and still produces a higher revision.
 */
export function ReleaseDialog({
  cluster,
  onClose,
  onAccepted,
}: {
  cluster: GatewayCluster;
  onClose: () => void;
  onAccepted: (revision: string) => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"current" | "rollback">("current");
  const [rollback, setRollback] = useState("");
  const release = useIdempotentMutation<ReleaseAccepted, { clusterId: string; rollback_revision: number | null }>({
    keyPrefix: "release",
    mutationFn: async ({ clusterId, rollback_revision }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/gateway-clusters/{cluster_id}/releases", {
            params: { path: { cluster_id: clusterId }, header: { "Idempotency-Key": idempotencyKey } },
            body: rollback_revision === null ? {} : { rollback_revision },
          }),
        )
      ).data,
    onSuccess: (accepted) => {
      toast.success(t("clusters.releaseAccepted", { revision: accepted.revision }));
      void queryClient.invalidateQueries({ queryKey: ["gateway-clusters"] });
      onAccepted(accepted.revision);
    },
  });
  const rollbackValid = /^\d+$/.test(rollback) && Number(rollback) > 0;
  const next = (BigInt(cluster.desired_revision) + BigInt(1)).toString();

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle>{t("clusters.releaseTitle", { name: cluster.name })}</DialogTitle>
          <DialogDescription>{t("clusters.releaseHint")}</DialogDescription>
        </DialogHeader>
        {release.error ? <ErrorAlert error={release.error} /> : null}
        <RadioGroup value={mode} onValueChange={(value) => setMode(value as "current" | "rollback")} className="gap-2">
          <Label className="items-start rounded-lg border px-3 py-2.5 font-normal">
            <RadioGroupItem value="current" className="mt-0.5" />
            <span className="flex flex-col gap-0.5">
              <span className="font-medium">{t("clusters.releaseCurrent")}</span>
              <span className="text-xs text-muted-foreground">{t("clusters.releaseCurrentHint", { revision: next })}</span>
            </span>
          </Label>
          <Label className="items-start rounded-lg border px-3 py-2.5 font-normal">
            <RadioGroupItem value="rollback" className="mt-0.5" />
            <span className="flex flex-col gap-1.5">
              <span className="font-medium">{t("clusters.releaseRollback")}</span>
              <span className="text-xs text-muted-foreground">{t("clusters.releaseRollbackHint")}</span>
              {mode === "rollback" ? (
                <Input
                  aria-label={t("clusters.rollbackRevision")}
                  inputMode="numeric"
                  value={rollback}
                  onChange={(event) => setRollback(event.target.value)}
                  className="h-8 w-32 font-mono"
                />
              ) : null}
            </span>
          </Label>
        </RadioGroup>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            disabled={release.isPending || (mode === "rollback" && !rollbackValid)}
            onClick={() =>
              release.mutate({ clusterId: cluster.id, rollback_revision: mode === "rollback" ? Number(rollback) : null })
            }
          >
            {release.isPending ? <Spinner /> : null}
            {mode === "current" ? t("clusters.releaseSubmit", { revision: next }) : t("clusters.releaseRollbackSubmit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
