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
    "authentication" .Values.ui.cluster.authentication
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
