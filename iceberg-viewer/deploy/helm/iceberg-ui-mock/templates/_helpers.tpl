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

{{- define "iceberg-ui-mock.mock.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iceberg-ui-mock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mock-backend
{{- end }}

{{- define "iceberg-ui-mock.mock.fullname" -}}
{{ include "iceberg-ui-mock.fullname" . }}-mock-backend
{{- end }}

{{- define "iceberg-ui-mock.postgres.selectorLabels" -}}
app.kubernetes.io/name: {{ include "iceberg-ui-mock.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end }}

{{/* Bare hostname the UI uses as `proxy` (port 80 is implicit, like the official chart). */}}
{{- define "iceberg-ui-mock.mock.proxyAddress" -}}
{{- if .Values.ui.cluster.proxy }}
{{- .Values.ui.cluster.proxy }}
{{- else }}
{{- printf "%s.%s.svc.cluster.local" (include "iceberg-ui-mock.mock.fullname" .) .Release.Namespace }}
{{- end }}
{{- end }}
