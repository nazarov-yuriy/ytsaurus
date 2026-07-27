{{- define "iceberg-ui-mock.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "iceberg-ui-mock.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Append a component suffix without truncating it away. This keeps resource names
distinct even when the release name or fullnameOverride reaches the DNS limit.
*/}}
{{- define "iceberg-ui-mock.suffixedFullname" -}}
{{- $context := index . 0 -}}
{{- $suffix := index . 1 -}}
{{- $maxLength := int (sub 63 (len $suffix)) -}}
{{- $base := include "iceberg-ui-mock.fullname" $context | trunc $maxLength | trimSuffix "-" -}}
{{- printf "%s%s" $base $suffix -}}
{{- end }}

{{- define "iceberg-ui-mock.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "iceberg-ui-mock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "iceberg-ui-mock.ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iceberg-ui-mock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: ui
{{- end }}

{{- define "iceberg-ui-mock.ui.fullname" -}}
{{ include "iceberg-ui-mock.suffixedFullname" (list . "-ui") }}
{{- end }}

{{- define "iceberg-ui-mock.ui.secretName" -}}
{{ include "iceberg-ui-mock.suffixedFullname" (list . "-ui-secret") }}
{{- end }}

{{- define "iceberg-ui-mock.mock.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iceberg-ui-mock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mock-backend
{{- end }}

{{- define "iceberg-ui-mock.mock.fullname" -}}
{{ include "iceberg-ui-mock.suffixedFullname" (list . "-mock-backend") }}
{{- end }}

{{- define "iceberg-ui-mock.mock.sourcesName" -}}
{{ include "iceberg-ui-mock.suffixedFullname" (list . "-mock-backend-src") }}
{{- end }}

{{- define "iceberg-ui-mock.postgres.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iceberg-ui-mock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end }}

{{- define "iceberg-ui-mock.postgres.fullname" -}}
{{ include "iceberg-ui-mock.suffixedFullname" (list . "-postgres") }}
{{- end }}

{{- define "iceberg-ui-mock.smoke.fullname" -}}
{{ include "iceberg-ui-mock.suffixedFullname" (list . "-smoke-test") }}
{{- end }}

{{/*
An explicit cluster authentication value wins, except that delegated
authentication may never be paired with "none". Otherwise PostgreSQL or an
upstream YTsaurus proxy enables password auth automatically; an all-in-RAM
anonymous deployment requires the explicit development opt-in.
*/}}
{{- define "iceberg-ui-mock.auth.mode" -}}
{{- if not (kindIs "bool" .Values.auth.allowAnonymous) -}}
{{- fail "auth.allowAnonymous must be a boolean" -}}
{{- end -}}
{{- $configured := .Values.ui.cluster.authentication -}}
{{- if eq $configured "none" -}}
{{- if .Values.auth.ytUpstream -}}
{{- fail "ui.cluster.authentication=none is incompatible with non-empty auth.ytUpstream" -}}
{{- else if not (eq .Values.auth.allowAnonymous true) -}}
{{- fail "authentication=none requires the explicit development-only auth.allowAnonymous=true opt-in" -}}
{{- end -}}
none
{{- else if eq $configured "basic" -}}
{{- if not (or .Values.postgres.enabled .Values.auth.ytUpstream) -}}
{{- fail "authentication=basic requires postgres.enabled=true or non-empty auth.ytUpstream" -}}
{{- end -}}
basic
{{- else if $configured -}}
{{- fail "ui.cluster.authentication supports only none or basic in this chart" -}}
{{- else if or .Values.postgres.enabled .Values.auth.ytUpstream -}}
basic
{{- else if eq .Values.auth.allowAnonymous true -}}
none
{{- else -}}
{{- fail "configure postgres or auth.ytUpstream, or explicitly opt in to development-only anonymous mode with auth.allowAnonymous=true" -}}
{{- end -}}
{{- end }}

{{- define "iceberg-ui-mock.auth.robotToken" -}}
{{- $token := required "auth.robotToken must be non-empty when authentication is enabled" .Values.auth.robotToken | toString -}}
{{- if eq $token "mock-robot-token" -}}
{{- fail "auth.robotToken must be changed from the published mock-robot-token default when authentication is enabled" -}}
{{- end -}}
{{- $token -}}
{{- end }}

{{- define "iceberg-ui-mock.ui.interfaceSecret" -}}
{{- if ne (include "iceberg-ui-mock.auth.mode" .) "none" -}}
{{ dict "oauthToken" (include "iceberg-ui-mock.auth.robotToken" .) | toJson }}
{{- else -}}
{}
{{- end -}}
{{- end }}

{{- define "iceberg-ui-mock.postgres.password" -}}
{{- $password := required "postgres.password must be non-empty when postgres.enabled=true and existingSecret is unset" .Values.postgres.password | toString -}}
{{- if eq $password "mock-password" -}}
{{- fail "postgres.password must be changed from the published mock-password default when postgres.enabled=true" -}}
{{- end -}}
{{- $password -}}
{{- end }}

{{/*
Roll both consumers when chart-managed credentials change. Helm cannot inspect
an external Secret, so users bump existingSecretRevision after rotating it.
*/}}
{{- define "iceberg-ui-mock.postgres.credentialsChecksum" -}}
{{- if .Values.postgres.existingSecret -}}
{{- printf "%s:%s" .Values.postgres.existingSecret .Values.postgres.existingSecretRevision | sha256sum -}}
{{- else -}}
{{- printf "%s:%s" (include "iceberg-ui-mock.postgres.fullname" .) (include "iceberg-ui-mock.postgres.password" .) | sha256sum -}}
{{- end -}}
{{- end }}

{{/* The UI proxy address. Port 80 is implicit; other Service ports are explicit. */}}
{{- define "iceberg-ui-mock.mock.proxyAddress" -}}
{{- if .Values.ui.cluster.proxy }}
{{- .Values.ui.cluster.proxy }}
{{- else }}
{{- $host := printf "%s.%s.svc.cluster.local" (include "iceberg-ui-mock.mock.fullname" .) .Release.Namespace -}}
{{- if eq (int .Values.mockBackend.service.port) 80 -}}
{{- $host -}}
{{- else -}}
{{- printf "%s:%d" $host (int .Values.mockBackend.service.port) -}}
{{- end }}
{{- end }}
{{- end }}

{{- define "iceberg-ui-mock.ui.clustersConfig" -}}
{{ dict "clusters" (list (dict
    "id" .Values.ui.cluster.id
    "name" .Values.ui.cluster.name
    "description" .Values.ui.cluster.description
    "environment" .Values.ui.cluster.environment
    "group" .Values.ui.cluster.group
    "theme" .Values.ui.cluster.theme
    "authentication" (include "iceberg-ui-mock.auth.mode" .)
    "secure" false
    "disableHeavyProxies" true
    "proxy" (include "iceberg-ui-mock.mock.proxyAddress" .)
  )) | toJson }}
{{- end }}

{{- define "iceberg-ui-mock.ui.commonConfig" -}}
module.exports = {
  uiSettings: {
    directDownload: {{ .Values.settings.directDownload | toJson }},
  },
};
{{- end }}
