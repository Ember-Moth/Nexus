{{/* Names */}}
{{- define "nexus.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "nexus.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "nexus.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "nexus.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "nexus.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "nexus.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: nexus
{{- end -}}

{{/* Selector labels for one component: include "nexus.selector" (list . "api") */}}
{{- define "nexus.selector" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: {{ include "nexus.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{/* Images */}}
{{- define "nexus.image" -}}
{{- $root := index . 0 -}}
{{- $image := index . 1 -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository (default $root.Chart.AppVersion $image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "nexus.pullSecrets" -}}
{{- with .Values.images.pullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Dependencies */}}
{{- define "nexus.postgresHost" -}}
{{- if .Values.postgresql.host -}}
{{- .Values.postgresql.host -}}
{{- else if eq .Values.postgresql.mode "cloudnative-pg" -}}
{{- printf "%s-rw.%s.svc" .Values.postgresql.clusterName .Release.Namespace -}}
{{- else -}}
{{- fail "postgresql.host is required with postgresql.mode=external" -}}
{{- end -}}
{{- end -}}

{{- define "nexus.postgresQuery" -}}
{{- printf "sslmode=%s&sslrootcert=/etc/nexus/postgres-ca/%s" .Values.postgresql.sslmode .Values.postgresql.ca.key -}}
{{- end -}}

{{- define "nexus.redisHost" -}}
{{- default (printf "nexus-redis.%s.svc" .Release.Namespace) .Values.redis.host -}}
{{- end -}}

{{- define "nexus.adminSecret" -}}
{{- default (printf "%s-superuser" .Values.postgresql.clusterName) .Values.secrets.database.admin.name -}}
{{- end -}}

{{/*
Environment that turns a role's Secret into the URL a Python process reads. Passwords from
basic-auth Secrets are spliced in with Kubernetes $(VAR) expansion, so they must be URL-safe.
Usage: include "nexus.databaseEnv" (list . "api" "NEXUS_DATABASE_URL")
*/}}
{{- define "nexus.databaseEnv" -}}
{{- $root := index . 0 -}}
{{- $role := index . 1 -}}
{{- $var := index . 2 -}}
{{- $secret := index $root.Values.secrets.database $role -}}
{{- if $secret.urlKey }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef: {name: {{ $secret.name }}, key: {{ $secret.urlKey }}}
{{- else }}
- name: NEXUS_DATABASE_PASSWORD_{{ upper $role }}
  valueFrom:
    secretKeyRef: {name: {{ $secret.name }}, key: password}
- name: {{ $var }}
  value: {{ printf "postgresql+psycopg://nexus_%s:$(NEXUS_DATABASE_PASSWORD_%s)@%s:%v/%s?%s" $role (upper $role) (include "nexus.postgresHost" $root) $root.Values.postgresql.port $root.Values.postgresql.database (include "nexus.postgresQuery" $root) | quote }}
{{- end }}
{{- end -}}

{{/* Administrator URL for the provisioning and relay-node Jobs (libpq form). */}}
{{- define "nexus.adminEnv" -}}
{{- $var := index . 1 -}}
{{- $root := index . 0 -}}
{{- if $root.Values.secrets.database.admin.urlKey }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef: {name: {{ include "nexus.adminSecret" $root }}, key: {{ $root.Values.secrets.database.admin.urlKey }}}
{{- else }}
- name: NEXUS_ADMIN_USER
  valueFrom:
    secretKeyRef: {name: {{ include "nexus.adminSecret" $root }}, key: username}
- name: NEXUS_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ include "nexus.adminSecret" $root }}, key: password}
- name: {{ $var }}
  value: {{ printf "postgresql://$(NEXUS_ADMIN_USER):$(NEXUS_ADMIN_PASSWORD)@%s:%v/%s?%s" (include "nexus.postgresHost" $root) $root.Values.postgresql.port $root.Values.postgresql.maintenanceDatabase (include "nexus.postgresQuery" $root) | quote }}
{{- end }}
{{- end -}}

{{- define "nexus.redisEnv" -}}
{{- $root := index . 0 -}}
{{- $var := index . 1 -}}
- name: NEXUS_REDIS_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.redis }}, key: password}
{{- if $root.Values.redis.tls }}
- name: {{ $var }}
  value: {{ printf "rediss://:$(NEXUS_REDIS_PASSWORD)@%s:%v/%v%s" (include "nexus.redisHost" $root) $root.Values.redis.port $root.Values.redis.db (ternary "?ssl_ca_certs=/etc/nexus/redis-ca/ca.crt" "" (eq (index . 2) "python")) | quote }}
{{- else }}
- name: {{ $var }}
  value: {{ printf "redis://:$(NEXUS_REDIS_PASSWORD)@%s:%v/%v" (include "nexus.redisHost" $root) $root.Values.redis.port $root.Values.redis.db | quote }}
{{- end }}
{{- end -}}

{{/* API and Worker secrets: encryption key ring, BFF secret, mail, Turnstile and R2 credentials. */}}
{{- define "nexus.controlPlaneSecretEnv" -}}
{{- $root := index . 0 -}}
- name: NEXUS_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.encryption }}, key: encryption-key}
- name: NEXUS_PREVIOUS_ENCRYPTION_KEYS
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.encryption }}, key: previous-encryption-keys, optional: true}
{{- if eq (index . 1) "api" }}
- name: NEXUS_AUTH_PROXY_SECRET
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.authProxy }}, key: secret}
{{- end }}
- name: NEXUS_SMTP_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.integrations }}, key: smtp-password, optional: true}
- name: NEXUS_RESEND_API_KEY
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.integrations }}, key: resend-api-key, optional: true}
- name: NEXUS_CLOUDFLARE_EMAIL_API_TOKEN
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.integrations }}, key: cloudflare-email-api-token, optional: true}
- name: NEXUS_TURNSTILE_SECRET_KEY
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.secrets.integrations }}, key: turnstile-secret-key, optional: true}
- name: NEXUS_OBJECT_STORAGE_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.objectStorage.credentialsSecret }}, key: ACCESS_KEY_ID}
- name: NEXUS_OBJECT_STORAGE_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef: {name: {{ $root.Values.objectStorage.credentialsSecret }}, key: ACCESS_SECRET_KEY}
{{- end -}}

{{/* Object storage startup parameters, added to a dict of environment values: (list $ $dict). */}}
{{- define "nexus.objectStorageConfig" -}}
{{- $root := index . 0 -}}
{{- $data := index . 1 -}}
{{- $storage := $root.Values.objectStorage -}}
{{- $_ := set $data "NEXUS_OBJECT_STORAGE_ENDPOINT_URL" (required "objectStorage.endpointURL is required: https://<account_id>.r2.cloudflarestorage.com" $storage.endpointURL) -}}
{{- $_ = set $data "NEXUS_OBJECT_STORAGE_BUCKET" (required "objectStorage.bucket is required" $storage.bucket) -}}
{{- $_ = set $data "NEXUS_OBJECT_STORAGE_REGION" (required "objectStorage.region is required" $storage.region) -}}
{{- end -}}

{{/* CA files every database and Redis client mounts. */}}
{{- define "nexus.caVolumes" -}}
- name: postgres-ca
{{- if .Values.postgresql.ca.configMap }}
  configMap:
    name: {{ .Values.postgresql.ca.configMap }}
    items: [{key: {{ .Values.postgresql.ca.key }}, path: {{ .Values.postgresql.ca.key }}}]
{{- else }}
  secret:
    secretName: {{ default (printf "%s-ca" .Values.postgresql.clusterName) .Values.postgresql.ca.secret }}
    items: [{key: {{ .Values.postgresql.ca.key }}, path: {{ .Values.postgresql.ca.key }}}]
{{- end }}
{{- if .Values.redis.tls }}
- name: redis-ca
{{- if .Values.redis.caConfigMap }}
  configMap:
    name: {{ .Values.redis.caConfigMap }}
    items: [{key: ca.crt, path: ca.crt}]
{{- else }}
  secret:
    secretName: {{ .Values.redis.caSecret }}
    items: [{key: ca.crt, path: ca.crt}]
{{- end }}
{{- end }}
{{- end -}}

{{- define "nexus.caMounts" -}}
- name: postgres-ca
  mountPath: /etc/nexus/postgres-ca
  readOnly: true
{{- if .Values.redis.tls }}
- name: redis-ca
  mountPath: /etc/nexus/redis-ca
  readOnly: true
{{- end }}
{{- end -}}

{{- define "nexus.relayGrace" -}}
{{- if .Values.relay.terminationGracePeriodSeconds -}}
{{- .Values.relay.terminationGracePeriodSeconds -}}
{{- else -}}
{{- add .Values.relay.preStopSeconds .Values.relay.drainSeconds 90 15 -}}
{{- end -}}
{{- end -}}

{{/* Hook Job pod defaults. */}}
{{- define "nexus.hookPod" -}}
restartPolicy: Never
automountServiceAccountToken: false
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- include "nexus.pullSecrets" . }}
{{- end -}}
